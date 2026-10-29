"""Ledger tickets behavior, using independent copied repositories."""

from __future__ import annotations

import unittest
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from contextlib import closing
from datetime import datetime, timezone
from typing import cast, final
from unittest import mock
import rules
import style_ledger
import style_plan
import berth_board
import coverage_report


from ledger_test_support import (LedgerTestCase, LedgerStatus)


@final
class StyleLedgerTicketsTests(LedgerTestCase):
    def test_widen_records_each_path_with_its_reason_name_and_time(self) -> None:
        ledger, issue = self.widen_ticket()
        extra, more = "crates/alpha/src/extra.rs", "crates/alpha/src/more.rs"
        result, output = self.widen(extra, more)
        self.assertEqual(result, 0, output)
        ledger.ticket_assign("names-1", "main")
        opened, = (cast(style_ledger.TicketRecord, json.loads(line))
                   for line in self.cli("ticket", "open", "--unit", "names").stdout.splitlines())
        widened = opened["widened"]
        self.assertEqual([(item["path"], item["reason"], item["widened_by"]) for item in widened],
                         [(path, "the move needs a new file", "showrunner") for path in (extra, more)])
        at = widened[0]["widened_at"]
        self.assertEqual(datetime.fromisoformat(at).utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(output, "".join(f"widened: {path} — the move needs a new file (showrunner, {at})\n"
                                         for path in (extra, more)))
        self.assertEqual(ledger.ticket_origin("names-1"), style_ledger.ApprovedProposal(
            issue, "names", tuple(opened["finding_ids"]), (extra, more)))
        rows = self.widened_rows(ledger)
        self.assertEqual(self.widen(extra), (0, output))
        self.assertEqual(self.widened_rows(ledger), rows)

    def test_widen_refuses_tickets_paths_and_reasons_it_cannot_record(self) -> None:
        ledger, issue = self.widen_ticket()
        self.add_ticket("read-1", [self.rule("read")])
        extra, more = "crates/alpha/src/extra.rs", "crates/alpha/src/more.rs"
        approved = issue.read_text()
        refusals: tuple[tuple[str, tuple[str, ...], dict[str, str], str], ...] = (
            ("unknown ticket", (extra,), {"ticket": "names-9"}, "unknown ticket names-9"),
            ("coverage ticket", (extra,), {"ticket": "read-1"},
             "read-1: only a proposal ticket widens; this is a read ticket"),
            ("empty reason", (extra,), {"reason": "  "}, "names-1: widen requires a reason"),
            ("empty name", (extra,), {"by": " "}, "names-1: widen requires --by"),
            ("absolute path", ("/etc/hosts",), {}, "path must be relative to the repository: /etc/hosts"),
            ("parent path", ("crates/../extra.rs",), {}, "path must be relative to the repository: crates/../extra.rs"),
            ("allowed path", (self.sources[0],), {},
             f"names-1: {self.sources[0]} is already allowed by the approved findings"),
        )
        for name, paths, options, message in refusals:
            with self.subTest(name):
                result, output = self.widen(*paths, **options)
                self.assertEqual((result, output), (2, f"style_plan: {message}\n"))
                self.assertEqual(self.widened_rows(ledger), [])
        with self.subTest("decision not approved"):
            _ = issue.write_text(approved.replace("status: open", "status: closed"))
            self.assertEqual(self.widen(extra), (2, "style_plan: proposal approval or filed findings changed\n"))
            self.assertEqual(self.widened_rows(ledger), [])
            _ = issue.write_text(approved)
        with self.subTest("another reason"):
            self.assertEqual(self.widen(extra)[0], 0)
            rows = self.widened_rows(ledger)
            self.assertEqual(self.widen(extra, reason="another reason"), (
                2, f"style_plan: names-1: {extra} is already widened for another reason: the move needs a new file\n"))
            self.assertEqual(self.widened_rows(ledger), rows)
        with self.subTest("merged ticket"):
            with closing(sqlite3.connect(ledger.database)) as connection:
                _ = connection.execute("UPDATE tickets SET state='merged' WHERE id='names-1'")
                connection.commit()
            self.assertEqual(self.widen(more), (2, "style_plan: names-1: cannot widen a merged ticket\n"))
            with closing(sqlite3.connect(ledger.database)) as connection:
                _ = connection.execute("UPDATE tickets SET state='queued' WHERE id='names-1'")
                connection.commit()
        with self.subTest("dropped ticket"):
            ledger.ticket_drop("names-1")
            self.assertEqual(self.widen(more), (2, "style_plan: names-1: cannot widen a dropped ticket\n"))
        self.assertEqual([row[1] for row in self.widened_rows(ledger)], [extra])

    def test_seen_tip_refreshes_listed_sites_after_rule_edit_without_another_sync(self) -> None:
        rule_id = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: first_marker\n  surface: whole\n",
        )
        self.write(self.sources[0], "pub fn first_marker() {}\n")
        self.write(self.sources[2], "pub fn second_marker() {}\n")
        tip = self.commit("Candidate markers")
        self.set_tip(tip)
        _ = self.cli("tip", "done", "--tip", tip)
        self.assertEqual(self.cli("tip", "seen", "--tip", tip).stdout.strip(), "seen")
        self.assertEqual({site.path for site in self.ledger().listed_sites(rule_id)}, {self.sources[0]})
        path = self.rule_dir / "listed.md"
        _ = path.write_text(path.read_text().replace("first_marker", "second_marker"))
        self.assertEqual(self.cli("tip", "seen", "--tip", tip).stdout.strip(), "seen")
        self.assertEqual({site.path for site in self.ledger().listed_sites(rule_id)}, {self.sources[2]})
        self.assertEqual({cell.path for cell in self.ledger().cells(rule_id)}, {self.sources[2]})

    def test_sync_without_style_branch_keeps_unmerged_judgments_awaiting_merge(self) -> None:
        rule_id = self.rule("read")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-qb", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked = self.commit("Correct style source")
        self.record(rule_id, self.sources[0], checked, "fixed")
        _ = self.git("checkout", "main")
        self.write("README.md", "Unrelated parent change\n")
        synced = self.commit("Advance parent")
        self.set_tip(synced)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "awaiting merge")

    def test_status_refreshes_changed_generator_at_the_same_tip(self) -> None:
        rule_id = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: first_marker\n  surface: whole\n",
        )
        self.write(self.sources[0], "pub fn first_marker() {}\n")
        self.write(self.sources[2], "pub fn second_marker() {}\n")
        tip = self.commit("Add distinct candidate markers")
        self.set_tip(tip)
        self.record(rule_id, self.sources[0], tip)
        ledger = self.ledger()
        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, {self.sources[0]})
        rule_path = self.rule_dir / "listed.md"
        _ = rule_path.write_text(rule_path.read_text().replace("first_marker", "second_marker"))

        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))

        self.assertEqual([(cell["rule"], cell["path"], cell["state"]) for cell in status["cells"]],
                         [(rule_id, self.sources[2], "open")])
        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, {self.sources[2]})
        self.assertEqual({cell.path for cell in ledger.cells()}, {self.sources[2]})

    def test_same_tip_refresh_adds_newly_classified_rules_and_removes_legacy_sites(self) -> None:
        rule_id = self.rule("legacy", "mechanism: llm\ncandidates:\n  kind: regex\n  pattern: value\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        self.assertEqual(ledger.listed_sites(rule_id), ())
        self.assertEqual(list(ledger.cells()), [])
        _ = self.rule(
            "legacy", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: whole\n",
        )

        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))

        self.assertEqual({cell["path"] for cell in status["cells"]}, set(self.sources))
        self.assertEqual(status["unclassified_rules"], [])
        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources))
        _ = self.rule("legacy", "mechanism: llm\ncandidates:\n  kind: regex\n  pattern: value\n")
        self.assertEqual(ledger.listed_sites(rule_id), ())
        self.assertEqual(list(ledger.cells()), [])

    def test_zero_site_markers_survive_repeated_reads_and_refresh_to_new_sites(self) -> None:
        rule_id = self.rule(
            "empty", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n"
            + "  pattern: absent_marker\n  surface: whole\n",
        )
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        for _ in range(2):
            self.assertEqual(ledger.listed_sites(rule_id), ())
            progress, = ledger.rule_counts(style_ledger.WholeRepository())
            self.assertTrue(progress.complete)
        with closing(sqlite3.connect(ledger.database)) as connection:
            markers = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT rule,rule_blob,tip FROM listed_rules",
            ).fetchall())
        self.assertEqual(markers, [(rule_id, self.blob(rule_id), self.initial_tip)])

        rule_path = self.rule_dir / "empty.md"
        _ = rule_path.write_text(rule_path.read_text().replace("absent_marker", "value"))

        self.assertEqual({site.path for site in ledger.listed_sites(rule_id)}, set(self.sources))
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertFalse(progress.complete)

    def test_pre_filter_also_reads_non_rust_files_named_by_other_listed_rules(self) -> None:
        _ = self.rule(
            "scripts", "tier: B\nfamily: values\ncandidates:\n  kind: regex\n"
            + "  pattern: cargo test\n  globs: ['*.sh']\n  surface: whole\n",
        )
        words = self.rule("words", "tier: B\nfamily: names\npre_filter: banned\n")
        self.write("check.sh", "cargo test # banned\n")
        self.write("unlisted.md", "banned\n")
        self.set_tip(self.commit("Add non-Rust word hits"))

        self.assertEqual([(site.path, site.line) for site in self.ledger().listed_sites(words)], [("check.sh", 1)])
        self.assertEqual(self.states(words), {"check.sh": "open"})

    def test_version_two_migration_preserves_judgments_and_fills_listed_sites(self) -> None:
        read_rule = self.rule("read")
        listed = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: whole\n",
        )
        state_file = self.root / "state/sample/ledger.sqlite"
        state_file.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state_file)) as connection:
            _ = connection.executescript(style_ledger.MIGRATIONS[1])
            _ = connection.executescript(style_ledger.MIGRATIONS[2])
            _ = connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (2)")
            _ = connection.execute("INSERT INTO sync VALUES (1, ?, ?)", (self.initial_tip, "refs/heads/style/main"))
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (read_rule, self.blob(read_rule), "file", self.git("rev-parse", f"{self.initial_tip}:{self.sources[0]}"),
                 self.sources[0], self.initial_tip, "clean", "", "old-ticket", "worker", "2026-10-16T00:00:00+00:00"),
            )
            connection.commit()

        ledger = self.ledger()

        self.assertEqual({cell.path: cell.state for cell in ledger.cells(read_rule)}, {
            self.sources[0]: "current", **dict.fromkeys(self.sources[1:], "open"),
        })
        self.assertEqual({site.path for site in ledger.listed_sites(listed)}, set(self.sources))
        with closing(sqlite3.connect(state_file)) as connection:
            versions = cast(list[tuple[int]], connection.execute("SELECT version FROM schema_version").fetchall())
            sync = cast(list[tuple[str, str]], connection.execute("SELECT tip,style_branch FROM sync").fetchall())
        self.assertEqual(versions, [(max(style_ledger.MIGRATIONS),)])
        self.assertEqual(sync, [(self.initial_tip, "refs/heads/style/main")])

    def test_file_judgment_becomes_stale_only_for_changed_committed_file(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))
        ledger = self.ledger()
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertTrue(progress.complete)
        self.assertEqual((progress.current, progress.total), (4, 4))

        self.write(self.sources[0], "pub fn value() -> u32 { 2 }\n")
        tip = self.commit("Change alpha source")
        self.set_tip(tip)

        self.assertEqual(self.states(rule_id), {**dict.fromkeys(self.sources, "current"), self.sources[0]: "stale"})
        self.record(rule_id, self.sources[0], tip)
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))

    def test_crate_scope_stales_sibling_files_but_not_the_other_crate(self) -> None:
        rule_id = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(rule_id, path, self.initial_tip)
        self.write(self.sources[0], "pub fn value() -> u32 { 2 }\n")
        self.set_tip(self.commit("Change one alpha file"))

        self.assertEqual(self.states(rule_id), {
            self.sources[0]: "stale", self.sources[1]: "stale",
            self.sources[2]: "current", self.sources[3]: "current",
        })

    def test_manifest_scope_ignores_source_changes_and_tracks_every_workspace_manifest(self) -> None:
        rule_id = self.rule("manifests", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.set_tip(self.initial_tip)
        for crate in ("crates/alpha", "crates/beta"):
            self.record(rule_id, crate, self.initial_tip)
        self.write(self.sources[0], "pub fn value() -> u32 { 3 }\n")
        self.set_tip(self.commit("Change source only"))
        self.assertEqual(self.states(rule_id), {"crates/alpha": "current", "crates/beta": "current"})

        manifest = self.repo / "crates" / "beta" / "Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[features]\nextra = []\n')
        self.set_tip(self.commit("Change beta manifest"))
        self.assertEqual(self.states(rule_id), {"crates/alpha": "stale", "crates/beta": "stale"})

    def test_record_fingerprints_git_objects_even_with_dirty_working_files(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        self.write(self.sources[0], "pub fn uncommitted() {}\n")

        self.record(rule_id, self.sources[0], self.initial_tip)

        self.assertEqual(self.states(rule_id)[self.sources[0]], "current")
        self.set_tip(self.commit("Commit previously dirty file"))
        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")

    def test_crate_and_manifest_fingerprints_ignore_dirty_working_inputs(self) -> None:
        crate_rule = self.rule("crate", "tier: C\nfamily: types\ninput_scope: crate\n")
        manifest_rule = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.set_tip(self.initial_tip)
        self.write(self.sources[0], "pub fn uncommitted() {}\n")
        manifest = self.repo / "crates" / "beta" / "Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[features]\nextra = []\n')

        self.record(crate_rule, self.sources[0], self.initial_tip)
        self.record(manifest_rule, "crates/alpha", self.initial_tip)

        self.assertEqual(self.states(crate_rule)[self.sources[0]], "current")
        self.assertEqual(self.states(manifest_rule)["crates/alpha"], "current")
        self.set_tip(self.commit("Commit changed crate and manifest"))
        self.assertEqual(self.states(crate_rule)[self.sources[0]], "stale")
        self.assertEqual(self.states(manifest_rule)["crates/alpha"], "stale")

    def test_rustfmt_ignored_files_are_excluded_and_unowned_sources_use_root_crate(self) -> None:
        rule_id = self.rule("read")
        self.write("rustfmt.toml", 'ignore = ["crates/alpha/src/other.rs"]\n')
        self.write("tools/check.rs", "fn main() {}\n")
        self.set_tip(self.commit("Add ignored source and standalone tool"))

        cells = self.cells(rule_id)

        self.assertEqual({cell.path for cell in cells}, {
            self.sources[0], self.sources[2], self.sources[3], "tools/check.rs",
        })
        self.assertEqual({cell.crate for cell in cells if cell.path == "tools/check.rs"}, {"."})

    def test_member_structure_rule_has_only_its_member_files(self) -> None:
        local_dir = self.repo / "docs" / "alpha" / "style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "structure.md").write_text("---\ntier: D\nfamily: modules\n---\n# Structure\n")
        local_id = "local:docs/alpha/style/structure.md"
        self.set_tip(self.initial_tip)

        self.assertEqual(self.states(local_id), dict.fromkeys(self.sources[:2], "open"))
        for path in self.sources[:2]:
            self.record(local_id, path, self.initial_tip)
        self.assertEqual(self.states(local_id), dict.fromkeys(self.sources[:2], "current"))

    def test_rule_edit_stales_all_its_cells_and_old_supplied_blob_cannot_refresh_them(self) -> None:
        rule_id = self.rule("read")
        old_blob = self.blob(rule_id)
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(rule_id, path, self.initial_tip, rule_blob=old_blob)
        path = self.rule_dir / "read.md"
        _ = path.write_text(path.read_text() + "Additional rule requirement\n")

        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))
        self.record(rule_id, self.sources[0], self.initial_tip, rule_blob=old_blob)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")
        self.record(rule_id, self.sources[0], self.initial_tip)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "current")

    def test_exception_held_and_waived_expire_when_their_input_changes(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        _ = self.ledger().hold(rule_id, self.sources[1], 1, "Required rename", "rename", "value", "amount", self.blob(rule_id), "outside")
        for path, verdict in zip(self.sources, ("exception", "held", "waived")):
            self.record(rule_id, path, self.initial_tip, verdict, detail="Reviewed decision")
        self.assertEqual(self.states(rule_id), {
            **dict.fromkeys(self.sources[:3], "current"), self.sources[3]: "open",
        })
        for path in self.sources[:3]:
            self.write(path, "pub fn changed() {}\n")
        self.set_tip(self.commit("Change reviewed inputs"))
        self.assertEqual(self.states(rule_id), {
            **dict.fromkeys(self.sources[:3], "stale"), self.sources[3]: "open",
        })

    def test_style_branch_judgment_awaits_merge_then_becomes_current(self) -> None:
        rule_id = self.rule("read")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        style_tip = self.commit("Correct source on style branch")
        self.record(rule_id, self.sources[0], style_tip, "fixed", detail=style_tip)

        self.assertEqual(self.states(rule_id)[self.sources[0]], "awaiting merge")

        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--ff-only", "style/main")
        self.set_tip(style_tip)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "current")

    def test_pending_judgment_expires_when_style_branch_inputs_change_again(self) -> None:
        rule_id = self.rule("read")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked_tip = self.commit("Correct source on style branch")
        self.record(rule_id, self.sources[0], checked_tip, "fixed", detail=checked_tip)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "awaiting merge")

        self.write(self.sources[0], "pub fn subsequently_changed() {}\n")
        _ = self.commit("Change checked source again")

        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")

    def test_pending_judgment_uses_explicit_style_branch_instead_of_worker_ref(self) -> None:
        rule_id = self.rule("read")
        _ = self.git("checkout", "-q", "-b", "style/main")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked = self.commit("Correct source")
        _ = self.git("branch", "worker", checked)
        self.write(self.sources[0], "pub fn changed_again() {}\n")
        advanced = self.commit("Advance style branch")
        self.record(rule_id, self.sources[0], checked, "fixed")

        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")
        self.record(rule_id, self.sources[0], advanced, "fixed")
        self.assertEqual(self.states(rule_id)[self.sources[0]], "awaiting merge")

    def test_unmerged_judgment_without_configured_style_branch_is_stale(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked = self.commit("Correct source")
        self.record(rule_id, self.sources[0], checked)

        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")

    def test_implicit_dependency_member_has_its_own_crate_and_fingerprint(self) -> None:
        rule_id = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        manifest = self.repo / "crates/alpha/Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[dependencies]\nhelper = { path = "../helper" }\n')
        self.write("crates/helper/Cargo.toml", '[package]\nname = "helper"\nversion = "0.1.0"\n')
        source = "crates/helper/src/lib.rs"
        self.write(source, "pub fn helper() {}\n")
        tip = self.commit("Add implicit member")
        self.set_tip(tip)
        self.record(rule_id, source, tip)

        self.assertEqual({cell.crate for cell in self.cells(rule_id) if cell.path == source}, {"crates/helper"})
        self.write(self.sources[0], "pub fn changed() {}\n")
        self.set_tip(self.commit("Change another member"))
        self.assertEqual(self.states(rule_id)[source], "current")

    def test_record_at_head_resolves_new_commit_on_an_open_ledger(self) -> None:
        rule_id = self.rule("read")
        ledger = self.ledger()
        blob = self.blob(rule_id)
        ledger.set_tip("HEAD")
        ledger.record(rule_id, self.sources[0], "HEAD", blob, "clean")
        self.write(self.sources[0], "pub fn changed() {}\n")
        _ = self.commit("Move HEAD")
        ledger.set_tip("HEAD")
        ledger.record(rule_id, self.sources[0], "HEAD", blob, "clean")

        self.assertEqual({cell.state for cell in ledger.cells(rule_id) if cell.path == self.sources[0]}, {"current"})

    def test_rerecording_older_ticket_selects_its_newest_verdict(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        _ = self.hold_finding(rule_id, 1, ticket="A")
        self.record(rule_id, self.sources[0], self.initial_tip, "held", ticket="A")
        self.record(rule_id, self.sources[0], self.initial_tip, "clean", ticket="B")
        self.record(rule_id, self.sources[0], self.initial_tip, "held", ticket="A")

        row = next(line.split("\t") for line in self.cli("status").stdout.splitlines() if line.startswith(rule_id + "\t"))
        self.assertEqual(row[5], "1")

    def test_manifest_cells_ignore_vendored_excluded_and_fixture_manifests(self) -> None:
        rule_id = self.rule("manifests", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        self.write("Cargo.toml", '[workspace]\nmembers = ["crates/*"]\nexclude = ["crates/excluded"]\n')
        paths = ("vendor/library/Cargo.toml", "crates/excluded/Cargo.toml", "fixtures/example/Cargo.toml")
        for path in paths:
            self.write(path, '[package]\nname = "outside"\nversion = "0.1.0"\n')
        tip = self.commit("Add manifests outside workspace")
        self.set_tip(tip)
        self.record(rule_id, "crates/alpha", tip)
        for path in paths:
            with self.subTest(manifest=path):
                self.write(path, '[package]\nname = "outside"\nversion = "0.2.0"\n')
                self.set_tip(self.commit("Change unrelated manifest"))
                self.assertEqual(self.states(rule_id)["crates/alpha"], "current")

    def test_root_crate_fingerprint_excludes_member_files(self) -> None:
        rule_id = self.rule("read", "tier: C\nfamily: types\ninput_scope: crate\n")
        manifest = self.repo / "Cargo.toml"
        _ = manifest.write_text('[package]\nname = "root"\nversion = "0.1.0"\n' + manifest.read_text())
        self.write("src/lib.rs", "pub fn root() {}\n")
        tip = self.commit("Add root package")
        self.set_tip(tip)
        self.record(rule_id, "src/lib.rs", tip)
        self.write(self.sources[0], "pub fn changed_member() {}\n")
        self.set_tip(self.commit("Change member source"))

        self.assertEqual(self.states(rule_id)["src/lib.rs"], "current")
        self.write("src/lib.rs", "pub fn changed_root() {}\n")
        self.set_tip(self.commit("Change root source"))
        self.assertEqual(self.states(rule_id)["src/lib.rs"], "stale")

    def test_repeated_record_and_set_tip_preserve_coverage_after_reopening(self) -> None:
        rule_id = self.rule("read")
        for _ in range(2):
            self.set_tip(self.initial_tip)
            self.record(rule_id, self.sources[0], self.initial_tip, ticket="unit-repeat")
        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))

        self.assertEqual(len(status["cells"]), len(self.sources))
        self.assertEqual(status["unclassified_rules"], [])
        self.assertEqual(self.states(rule_id), {
            self.sources[0]: "current", **dict.fromkeys(self.sources[1:], "open"),
        })
        state_file = self.root / "state" / "sample" / "ledger.sqlite"
        self.assertTrue(state_file.is_file())
        with closing(sqlite3.connect(state_file)) as connection:
            rows = cast(list[tuple[int]], connection.execute("SELECT COUNT(*) FROM judgments").fetchall())
        self.assertEqual(rows, [(1,)])

    def test_migration_preserves_judgments_and_requires_an_explicit_style_branch(self) -> None:
        rule_id = self.rule("read")
        blob = self.blob(rule_id)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checked = self.commit("Correct source")
        state_file = self.root / "state/sample/ledger.sqlite"
        state_file.parent.mkdir(parents=True)
        with closing(sqlite3.connect(state_file)) as connection:
            _ = connection.executescript(style_ledger.MIGRATIONS[1])
            _ = connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (1)")
            _ = connection.execute("INSERT INTO sync VALUES (1, ?)", (self.initial_tip,))
            _ = connection.execute(
                'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,recorded_at,branch) '
                + 'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (rule_id, blob, "file", self.git("rev-parse", f"{checked}:{self.sources[0]}"), self.sources[0], checked,
                 "fixed", checked, "old-ticket", "worker", "2026-10-16T00:00:00+00:00", "refs/heads/style/main"),
            )
            connection.commit()

        ledger = self.ledger()

        self.assertEqual({cell.state for cell in ledger.cells(rule_id) if cell.path == self.sources[0]}, {"stale"})
        ledger.set_tip(self.initial_tip, style_branch="style/main")
        self.assertEqual({cell.state for cell in ledger.cells(rule_id) if cell.path == self.sources[0]}, {"awaiting merge"})
        ledger.set_tip(checked)
        self.assertEqual({cell.state for cell in ledger.cells(rule_id) if cell.path == self.sources[0]}, {"current"})

    def test_lint_records_hash_tool_configuration_and_replace_repeated_runs(self) -> None:
        rule_id = self.rule("lint", "tier: A\nmechanism: clippy\n")
        self.set_tip(self.initial_tip)
        self.lint(self.initial_tip, "clippy", True, {})
        state_file = self.root / "state" / "sample" / "ledger.sqlite"
        with closing(sqlite3.connect(state_file)) as connection:
            initial = cast(list[tuple[str]], connection.execute(
                "SELECT config_hash FROM lint_runs WHERE tool = 'clippy'",
            ).fetchall())
        for relative, content in (
            ("clippy.toml", 'disallowed-names = ["placeholder"]\n'),
            ("Cargo.toml", '[workspace]\nmembers = ["crates/alpha", "crates/beta"]\n'
             + '[workspace.lints.clippy]\nall = "warn"\n'),
        ):
            with self.subTest(config=relative):
                self.write(relative, content)
                tip = self.commit("Change lint configuration")
                self.set_tip(tip)
                self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))
                self.lint(tip, "clippy", True, {})
                self.lint(tip, "clippy", True, {})
                with closing(sqlite3.connect(state_file)) as connection:
                    runs = cast(list[tuple[str, str]], connection.execute(
                        "SELECT tip, config_hash FROM lint_runs WHERE tool = 'clippy' ORDER BY id",
                    ).fetchall())
                matching = [config_hash for run_tip, config_hash in runs if run_tip == tip]
                self.assertEqual(len(matching), 1)
                self.assertNotEqual(matching[0], initial[-1][0])
                initial = [(matching[0],)]
                self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))

    def test_tier_a_requires_success_and_uses_only_its_own_tools_diagnostics(self) -> None:
        rule_id = self.rule("lint", "tier: A\nmechanism: clippy\n")
        self.set_tip(self.initial_tip)
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "open"))
        self.lint(self.initial_tip, "mend", True, {})
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "open"))
        self.lint(self.initial_tip, "clippy", False, {})
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))
        self.lint(self.initial_tip, "clippy", True, {self.sources[0]: 1})
        self.assertEqual(self.states(rule_id), {
            self.sources[0]: "stale", **dict.fromkeys(self.sources[1:], "current"),
        })
        self.lint(self.initial_tip, "clippy", True, {})
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))
        self.lint(self.initial_tip, "clippy", False, {})
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))

    def test_lint_run_matches_tree_across_empty_commits_and_expires_on_source_change(self) -> None:
        rule_id = self.rule("format", "tier: A\nmechanism: rustfmt\n")
        self.set_tip(self.initial_tip)
        self.lint(self.initial_tip, "rustfmt", True, {})
        _ = self.git("commit", "--allow-empty", "-q", "-m", "Same tree")
        self.set_tip(self.git("rev-parse", "HEAD"))
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "current"))

        self.write(self.sources[0], "pub fn reformatted() {}\n")
        self.set_tip(self.commit("Change linted tree"))
        self.assertEqual(self.states(rule_id), dict.fromkeys(self.sources, "stale"))

    def test_all_legacy_catalog_has_no_cells_or_done_rules_and_names_the_action(self) -> None:
        legacy = self.rule("old", "mechanism: llm\ncandidates:\n  kind: regex\n  pattern: word\n")
        tool = self.rule("old-tool", "mechanism: clippy\n")
        self.set_tip(self.initial_tip)
        ledger = self.ledger()

        self.assertEqual(list(ledger.cells()), [])
        self.assertEqual(list(ledger.rule_counts(style_ledger.WholeRepository())), [])
        self.assertEqual(set(ledger.unclassified_rules()), {legacy, tool})
        self.assertIn("unclassified: 2 rules — pull nate_style", self.cli("status").stdout)
        status = cast(LedgerStatus, json.loads(self.cli("status", "--json").stdout))
        self.assertEqual(status["cells"], [])
        self.assertEqual(set(status["unclassified_rules"]), {legacy, tool})
        refusal = self.cli(
            "record", "--rule", legacy, "--path", self.sources[0], "--commit", self.initial_tip,
            "--rule-blob", self.blob(legacy), "--verdict", "clean", "--ticket", "unit-1",
            "--worker", "test-worker", success=False,
        )
        self.assertEqual(refusal.returncode, 2)
        self.assertIn(legacy, refusal.stderr)
        self.assertEqual(len(refusal.stderr.splitlines()), 1)

    def test_mixed_catalog_does_not_turn_legacy_rules_into_current_or_done(self) -> None:
        legacy = self.rule("old", "mechanism: llm\n")
        classified = self.rule("read")
        self.set_tip(self.initial_tip)
        for path in self.sources:
            self.record(classified, path, self.initial_tip)
        ledger = self.ledger()

        self.assertEqual({cell.rule for cell in ledger.cells()}, {classified})
        self.assertEqual(ledger.unclassified_rules(), (legacy,))
        progress, = ledger.rule_counts(style_ledger.WholeRepository())
        self.assertEqual(progress.rule_id, classified)
        self.assertTrue(progress.complete)
        self.assertIn("unclassified: 1 rules — pull nate_style", self.cli("status").stdout)

    def test_same_stem_global_and_member_rules_have_independent_cells_and_record_reach(self) -> None:
        global_id = self.rule("shared")
        local_dir = self.repo / "docs" / "alpha" / "style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "shared.md").write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Local\n")
        local_id = "local:docs/alpha/style/shared.md"
        self.set_tip(self.initial_tip)

        self.assertEqual(self.states(global_id), dict.fromkeys(self.sources, "open"))
        self.assertEqual(self.states(local_id), dict.fromkeys(self.sources[:2], "open"))
        self.record(local_id, self.sources[0], self.initial_tip)
        self.assertEqual(self.states(global_id), dict.fromkeys(self.sources, "open"))
        self.assertEqual(self.states(local_id)[self.sources[0]], "current")
        for invalid_id, path in ((local_id, self.sources[2]), ("shared", self.sources[0]), ("global:rust/absent.md", self.sources[0])):
            with self.subTest(rule=invalid_id, path=path):
                refusal = self.cli(
                    "record", "--rule", invalid_id, "--path", path, "--commit", self.initial_tip,
                    "--rule-blob", self.blob(local_id), "--verdict", "clean", "--ticket", "unit-1",
                    "--worker", "test-worker", success=False,
                )
                self.assertEqual(refusal.returncode, 2)
                self.assertIn(invalid_id, refusal.stderr)
                self.assertEqual(len(refusal.stderr.splitlines()), 1)
        filtered = cast(LedgerStatus, json.loads(self.cli(
            "status", "--rule", local_id, "--crate", "crates/alpha", "--json",
        ).stdout))
        self.assertEqual({(cell["rule"], cell["path"]) for cell in filtered["cells"]}, {
            (local_id, path) for path in self.sources[:2]
        })

    def test_checkpoint_restamps_own_family_verdicts_after_only_ticket_edits_and_refill_does_not_reissue(self) -> None:
        rule = self.rule("read")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        self.record(rule, path, self.initial_tip, "fixed", ticket="names-1")
        self.record(rule, path, self.initial_tip, ticket="another-ticket")
        self.write(path, "pub fn renamed() -> u32 { 1 }\n")
        _ = self.commit("Ticket renames the function")
        self.write(path, "pub fn renamed() -> u32 { 2 }\n")
        checkpoint = self.commit("Ticket checkpoint")

        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "current")
        tickets = style_plan.candidate_tickets(
            self.repo, ledger, style_plan.BusyFiles(berth_board.Board(()), frozenset()), "names",
            base=style_plan.NoUnitBranch())
        self.assertFalse(any(path in ticket.files for ticket in tickets))
        with closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str, str]], connection.execute(
                'SELECT "commit",verdict FROM judgments WHERE ticket=? ORDER BY id', ("names-1",)).fetchall())
            other_commits = cast(list[tuple[str]], connection.execute(
                'SELECT "commit" FROM judgments WHERE ticket=?', ("another-ticket",)).fetchall())
        self.assertEqual(rows, [(self.initial_tip, "fixed"), (checkpoint, "fixed")])
        self.assertEqual(other_commits, [(self.initial_tip,)])

    def test_set_tip_holds_no_write_lock_while_it_lists_sites(self) -> None:
        listed = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: marker\n  surface: whole\n",
        )
        self.set_tip(self.initial_tip)
        self.write(self.sources[0], "pub fn marker() {}\n")
        tip = self.commit("Add a marker")
        self.record_while_stalled("enumerate_candidates", lambda ledger: ledger.set_tip(tip))
        ledger = self.ledger()
        self.assertEqual(ledger.synced_commit(), tip)
        self.assertEqual({site.path for site in ledger.listed_sites(listed)}, {self.sources[0]})

    def test_checkpoint_holds_no_write_lock_while_it_restamps(self) -> None:
        rule = self.rule("read")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        _ = ledger.ticket_base("names-1")
        self.record(rule, path, self.initial_tip, "fixed", ticket="names-1")
        self.write(path, "pub fn renamed() -> u32 { 1 }\n")
        checkpoint = self.commit("Ticket checkpoint")
        self.record_while_stalled("_fingerprint", lambda writer: writer.ticket_checkpoint("names-1", checkpoint))
        self.assertEqual(ledger.ticket_open("names")[0]["checkpoint"], checkpoint)
        self.assertIn(checkpoint, {judgment.commit for judgment in ledger.latest_ticket_judgments("names-1")})

    def test_mark_built_holds_no_write_lock_while_it_checks_the_code(self) -> None:
        rule, issue, _ = self.decision_findings()
        ledger = self.ledger()
        finding = ledger.hold(rule, self.sources[0], 3, "value", "type", "u32", "u64", self.blob(rule), "outside")
        ledger.file_held(finding.finding_id, issue)
        ledger.ticket_add("names-1", "names", "proposal", "names", "crates/alpha", [self.sources[0]], [rule],
                          proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("names-1", "main")
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_checkpoint("names-1", self.initial_tip)
        _ = ledger.ticket_merge("names-1")
        self.record_while_stalled("applied_check", lambda writer: writer.mark_built(finding.finding_id))
        built = next(item for item in ledger.proposal_findings(issue) if item.finding_id == finding.finding_id)
        self.assertIsInstance(built.state, style_ledger.Built)

    def test_decline_holds_no_write_lock_while_it_checks_judged_inputs(self) -> None:
        rule = self.rule("read")
        path = self.sources[0]
        self.set_tip(self.initial_tip)
        ledger = self.ledger()
        finding = ledger.hold(rule, path, 1, "Rename value", "rename", "value", "amount", self.blob(rule), "outside")
        ledger.record(rule, path, self.initial_tip, self.blob(rule), "held", ticket="outside")
        self.record_while_stalled("_fingerprint", lambda writer: writer.decline_findings((finding.finding_id,), "declined", style_ledger.WaiveByPerson("user")))
        self.assertIsInstance(ledger.held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Waived)
        self.assertIsInstance(ledger.newest_held_judgment(rule, path), style_ledger.NoHeldJudgment)

    def test_checkpoint_starts_again_when_its_judgments_change_before_the_lock(self) -> None:
        ledger, checkpoint, load = self.judge_while_loading_rules(1)
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(ledger.ticket_open("names")[0]["checkpoint"], checkpoint)
        self.assertEqual({judgment.commit for judgment in ledger.latest_ticket_judgments("names-1")}, {checkpoint})

    def test_checkpoint_refuses_after_its_judgments_change_on_every_attempt(self) -> None:
        ledger, checkpoint, load = self.judge_while_loading_rules(style_ledger.LOCK_ATTEMPTS)
        with self.assertRaisesRegex(style_ledger.ChangedBeforeLock, "names-1: the ticket changed while it became checkpointed; rerun"):
            ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(load.call_count, style_ledger.LOCK_ATTEMPTS)
        self.assertEqual(ledger.ticket_open("names")[0]["state"], "assigned")

    def test_checkpoint_keeps_verdicts_stale_and_reissues_when_an_outside_commit_changed_the_path(self) -> None:
        rule = self.rule("read")
        path = self.sources[0]
        self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, "pub fn outside_change() {}\n")
        base = self.commit("Outside edit before ticket starts")
        ledger = self.ledger()
        ledger.set_tip(base, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), base)
        self.write(path, "pub fn ticket_change() {}\n")
        checkpoint = self.commit("Ticket checkpoint")

        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")
        tickets = style_plan.candidate_tickets(
            self.repo, ledger, style_plan.BusyFiles(berth_board.Board(()), frozenset()), "names",
            base=style_plan.NoUnitBranch())
        self.assertTrue(any(path in ticket.files for ticket in tickets))

    def test_checkpoint_does_not_restamp_a_path_changed_on_a_merged_outside_branch(self) -> None:
        rule = self.rule("read")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        self.record(rule, path, self.initial_tip, ticket="names-1")
        _ = self.git("checkout", "-q", "-b", "outside")
        self.write(path, "pub fn outside_change() {}\n")
        _ = self.commit("Outside branch edit")
        _ = self.git("checkout", "-q", "main")
        self.write("README.md", "Ticket work\n")
        _ = self.commit("Ticket work")
        _ = self.git("merge", "--no-ff", "-q", "outside", "-m", "Bring in outside work")
        checkpoint = self.git("rev-parse", "HEAD")
        ledger.ticket_checkpoint("names-1", checkpoint)
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")

    def test_removed_cell_keeps_the_deleting_commit_and_closes_the_cell_without_a_verdict_on_code(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, removed = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        self.record(rule, removed, self.initial_tip, ticket="earlier-1")
        (self.repo / removed).unlink()
        deleting = self.commit("Ticket deletes the file")
        self.write("README.md", "Ticket work after the delete\n")
        checkpoint = self.commit("Ticket checkpoint")
        self.assertEqual(ledger.unjudged_ticket_cells("names-1", "HEAD", self.in_worktree), (style_ledger.UnjudgedTicketCell(rule, removed),))

        self.record_removed(ledger, rule, removed, checkpoint)

        state = f"removed by ticket names-1 at {deleting[:7]}"
        expected = style_ledger.RemovedCell(rule=rule, path=removed, commit=deleting, state=state)
        opened = cast(dict[str, object], json.loads(self.cli("ticket", "open", "--unit", "names").stdout))
        self.assertEqual(opened["removed"], [dict(expected)])
        self.assertEqual(ledger.ticket_open("names")[0]["removed"], (expected,))
        self.assertIn(f"{rule} {removed}: {state}", coverage_report.ticket_line(ledger.ticket_open("names")[0]).splitlines())
        self.assertEqual(ledger.unjudged_ticket_cells("names-1", "HEAD", self.in_worktree), ())

        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        ledger.set_tip(checkpoint, "main")
        # The restamp carries the kept verdict to the checkpoint and leaves the removed row at its deleting commit.
        self.assertEqual(self.cell_rows(ledger, removed), [(self.initial_tip, "clean"), (deleting, "removed")])
        self.assertEqual(self.cell_rows(ledger, kept), [(self.initial_tip, "clean"), (checkpoint, "clean")])
        self.assertEqual(self.states(rule), {kept: "current", self.sources[2]: "open", self.sources[3]: "open"})

        # A file at the same path later is a new cell: the verdict of the file before the delete does not judge it.
        self.write(removed, "pub fn value() -> u32 { 1 }\n")
        ledger.set_tip(self.commit("Bring the file back"), "main")
        self.assertEqual(self.states(rule)[removed], "open")

    def test_checkpoint_names_a_cell_neither_recorded_nor_removed(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        for path in self.sources[:2]:
            (self.repo / path).unlink()
        checkpoint = self.commit("Ticket deletes both files")
        self.record_removed(ledger, rule, self.sources[0], checkpoint)
        # The style branch holds the delete and the synced tip does not, so the cell awaits merge; it is not current.
        self.assertEqual({path: self.states(rule)[path] for path in self.sources[:2]},
                         {self.sources[0]: "awaiting merge", self.sources[1]: "open"})
        counts = next(row for row in ledger.rule_counts(style_ledger.WholeRepository()) if row.rule_id == rule)
        self.assertEqual((counts.current, counts.awaiting_merge, counts.held), (0, 1, 0))
        refused = self.cli("ticket", "checkpoint", "--id", "names-1", "--commit", checkpoint, success=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: names-1: unjudged cells: {rule} {self.sources[1]}")
        self.record_removed(ledger, rule, self.sources[1], checkpoint)
        ledger.ticket_checkpoint("names-1", checkpoint)
        self.assertEqual(ledger.ticket_open("names")[0]["state"], "checkpointed")

    def test_a_verdict_recorded_after_a_removal_on_an_older_commit_does_not_judge_the_file_brought_back(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        path = self.sources[1]
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        self.record_removed(ledger, rule, path, deleting)
        # The late verdict judges the file before the delete; the order of recording does not move the removal.
        self.record(rule, path, self.initial_tip, ticket="earlier-1")
        self.write(path, original)
        ledger.set_tip(self.commit("Bring the same file back"), "main")
        self.assertEqual(self.states(rule)[path], "open")

    def test_removed_keeps_the_deleting_commit_and_not_a_later_merge_of_other_work(self) -> None:
        ledger, rule, base = self.removed_ticket()
        path = self.sources[1]
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        _ = self.git("checkout", "-q", "-b", "side", base)
        self.write("README.md", "Side work\n")
        _ = self.commit("Side work keeps the file")
        _ = self.git("checkout", "-q", "main")
        # The merge differs from its second parent at the path, but its diff against its first parent deletes nothing.
        _ = self.git("merge", "--no-ff", "-q", "side", "-m", "Bring in side work")
        self.record_removed(ledger, rule, path, "HEAD")
        self.assertEqual(self.cell_rows(ledger, path), [(deleting, "removed")])

    def test_removed_records_an_own_delete_of_a_file_that_merged_in_work_edited_and_prints_its_line(self) -> None:
        ledger, rule, base = self.removed_ticket()
        path = self.sources[1]
        _ = self.git("checkout", "-q", "-b", "outside", base)
        self.write(path, "pub fn edited_outside() {}\n")
        _ = self.commit("Outside work edits the file")
        _ = self.git("checkout", "-q", "main")
        # The normal sync: a first-parent merge brings the outside edit onto the ticket branch.
        _ = self.git("merge", "--no-ff", "-q", "outside", "-m", "Bring in outside work")
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        recorded = self.cli("record", "--rule", rule, "--path", path, "--commit", "HEAD", "--rule-blob", self.blob(rule),
                            "--verdict", "removed", "--ticket", "names-1", "--worker", "test-worker")
        self.assertEqual(recorded.stdout, f"{rule} {path}: removed by ticket names-1 at {deleting[:7]}\n")
        self.assertEqual(self.cell_rows(ledger, path), [(deleting, "removed")])

    def test_checkpoint_names_a_removed_cell_whose_file_is_back(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, path = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        self.record_removed(ledger, rule, path, deleting)
        self.write(path, original)
        restored = self.commit("Ticket brings the file back")
        self.assertEqual(ledger.unjudged_ticket_cells("names-1", "HEAD", self.in_worktree), (style_ledger.UnjudgedTicketCell(rule, path),))
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: unjudged cells: {re.escape(rule)} {re.escape(path)}$"):
            ledger.ticket_checkpoint("names-1", restored)
        self.record(rule, path, restored, ticket="names-1")
        ledger.ticket_checkpoint("names-1", restored)
        self.assertEqual(ledger.ticket_open("names")[0]["state"], "checkpointed")

    def test_checkpoint_does_not_count_a_verdict_of_the_ticket_on_a_commit_before_its_removal(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, path = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        self.record_removed(ledger, rule, path, deleting)
        # The ticket's newest row judges the file before the delete, so coverage reads the file brought back as open.
        self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, original)
        restored = self.commit("Ticket brings the file back")
        ledger.set_tip(restored, "main")
        self.assertEqual(self.states(rule)[path], "open")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: unjudged cells: {re.escape(rule)} {re.escape(path)}$"):
            ledger.ticket_checkpoint("names-1", restored)

    def test_checkpoint_counts_a_removal_by_another_ticket(self) -> None:
        ledger, rule, path, restored = self.removed_by_another_ticket()
        ledger.set_tip(restored, "main")
        self.assertEqual(self.states(rule)[path], "open")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: unjudged cells: {re.escape(rule)} {re.escape(path)}$"):
            ledger.ticket_checkpoint("names-1", restored)

    def test_restamp_does_not_carry_a_verdict_across_a_removal_by_another_ticket(self) -> None:
        ledger, rule, path, restored = self.removed_by_another_ticket()
        self.record(rule, path, restored, ticket="names-1")
        # The ticket's newest row judges the file before the other ticket's delete.
        self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, "fn edited() {}\n")
        checkpoint = self.commit("Ticket edits the file brought back")
        ledger.ticket_checkpoint("names-1", checkpoint)
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")

    def test_restamp_rechecks_a_removal_recorded_before_the_write_lock(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, path = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        ledger.ticket_add("other-names-1", "other-names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("other-names-1", "main")
        _ = ledger.ticket_base("other-names-1")
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        deleting = self.commit("Another ticket deletes the file")
        self.write(path, original)
        restored = self.commit("The file comes back")
        self.record(rule, path, restored, ticket="names-1")
        # The ticket's newest row judges the file before the delete, which no ticket has recorded yet.
        self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, "fn edited() {}\n")
        checkpoint = self.commit("Ticket edits the file brought back")
        prepared = style_ledger.Ledger.checkpoint_restamps
        other = self.ledger()

        def remove_after_prepare(owner: style_ledger.Ledger, ticket: style_ledger.TicketRecord,
                                 commit: str) -> style_ledger.CheckpointRestamps | style_ledger.NoRestamps:
            restamps = prepared(owner, ticket, commit)
            if (deleting, "removed") not in self.cell_rows(other, path):
                self.record_removed(other, rule, path, deleting, ticket="other-names-1")
            return restamps

        with mock.patch.object(style_ledger.Ledger, "checkpoint_restamps", autospec=True, side_effect=remove_after_prepare):
            ledger.ticket_checkpoint("names-1", checkpoint)
        # The checkpoint passes by the verdict on the restored file, and the verdict before the delete does not carry.
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")

    def test_restamp_rechecks_a_removal_that_takes_the_id_of_a_yielded_one(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        kept, path = self.sources[:2]
        self.record(rule, kept, self.initial_tip, ticket="names-1")
        for ticket in ("other-names-1", "third-names-1"):
            ledger.ticket_add(ticket, ticket.removesuffix("-1"), "read", "names", "crates/alpha", [path], [rule])
            ledger.ticket_assign(ticket, "main")
            _ = ledger.ticket_base(ticket)
        original = (self.repo / path).read_text()
        (self.repo / path).unlink()
        first = self.commit("Another ticket deletes the file")
        self.write(path, original)
        restored = self.commit("The file comes back")
        (self.repo / path).unlink()
        second = self.commit("A third ticket deletes the file")
        self.write(path, original)
        restored_again = self.commit("The file comes back again")
        self.record(rule, path, restored_again, ticket="names-1")
        # The ticket's newest row judges the file between the two deletes.
        self.record(rule, path, restored, ticket="names-1")
        # The first removal is the newest row, so the next row can take its id once the yield deletes it.
        self.record_removed(ledger, rule, path, first, ticket="other-names-1")
        self.write(path, "fn edited() {}\n")
        checkpoint = self.commit("Ticket edits the file brought back")
        prepared = style_ledger.Ledger.checkpoint_restamps
        other = self.ledger()

        def swap_after_prepare(owner: style_ledger.Ledger, ticket: style_ledger.TicketRecord,
                               commit: str) -> style_ledger.CheckpointRestamps | style_ledger.NoRestamps:
            restamps = prepared(owner, ticket, commit)
            if (second, "removed") not in self.cell_rows(other, path):
                _ = other.ticket_yield("other-names-1", path)
                self.record_removed(other, rule, path, second, ticket="third-names-1")
            return restamps

        with mock.patch.object(style_ledger.Ledger, "checkpoint_restamps", autospec=True, side_effect=swap_after_prepare):
            ledger.ticket_checkpoint("names-1", checkpoint)
        # The checkpoint passes by the verdict after the second delete, and the verdict before it does not carry.
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(rule)[path], "stale")

    def test_removed_refuses_a_file_still_present_outside_the_ticket_or_its_start(self) -> None:
        self.write("README.md", "Work before the ticket\n")
        _ = self.commit("Work before the ticket")
        ledger, rule, base = self.removed_ticket()
        path = self.sources[1]
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^{re.escape(path)}: still in the tree at {base[:7]}$"):
            self.record_removed(ledger, rule, path, base)
        self.write(path, "pub fn changed() {}\n")
        edited = self.commit("Ticket edits the file")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^{re.escape(path)}: still in the tree at {edited[:7]}$"):
            self.record_removed(ledger, rule, path, edited)
        (self.repo / self.sources[2]).unlink()
        outside = self.commit("Delete a file outside the ticket")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: {re.escape(rule)} {re.escape(self.sources[2])} is outside ticket cells$"):
            self.record_removed(ledger, rule, self.sources[2], outside)
        _ = self.git("checkout", "-q", "-b", "side", self.initial_tip)
        (self.repo / path).unlink()
        side = self.commit("Delete on a branch not under the start")
        _ = self.git("checkout", "-q", "main")
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: {side[:7]} is not under the ticket's start {base[:7]}$"):
            self.record_removed(ledger, rule, path, side)
        with self.assertRaisesRegex(style_ledger.LedgerError, "^removed: --reads requires clean, exception or held$"):
            ledger.record(rule, path, side, "", "removed", ticket="names-1", worker="test-worker", reads=(self.sources[0],))
        ledger.ticket_add("names-2", "names", "read", "names", "crates/alpha", [path], [rule])
        with self.assertRaisesRegex(style_ledger.LedgerError, "^names-2: no start commit, so its own commits are not known$"):
            self.record_removed(ledger, rule, path, outside, ticket="names-2")
        with self.assertRaisesRegex(style_ledger.LedgerError, "^unknown ticket other-1$"):
            self.record_removed(ledger, rule, path, outside, ticket="other-1")
        self.assertEqual(self.cell_rows(ledger, path), [])

    def test_removed_refuses_a_file_that_a_merged_in_commit_deleted(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        path = self.sources[1]
        _ = self.git("checkout", "-q", "-b", "outside")
        (self.repo / path).unlink()
        _ = self.commit("Outside branch deletes the file")
        _ = self.git("checkout", "-q", "main")
        self.write("README.md", "Ticket work\n")
        _ = self.commit("Ticket work")
        _ = self.git("merge", "--no-ff", "-q", "outside", "-m", "Bring in outside work")
        merge = self.git("rev-parse", "HEAD")
        # The merge's diff against its first parent deletes the path, so the delete is not the ticket's.
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^{re.escape(path)}: merge {merge[:7]} deletes it, not a commit of names-1's own$"):
            self.record_removed(ledger, rule, path, "HEAD")
        self.assertEqual(self.cell_rows(ledger, path), [])

    def test_removed_refuses_a_file_deleted_before_the_ticket_start(self) -> None:
        rule = self.rule("read")
        path = self.sources[1]
        (self.repo / path).unlink()
        base = self.commit("Delete before the ticket starts")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [rule])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), base)
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^{re.escape(path)}: not a file at the ticket's start {base[:7]}$"):
            self.record_removed(ledger, rule, path, base)
        # A structure ticket's crate cell is a folder, so its removal is out of scope.
        ledger.ticket_add("modules-1", "modules", "structure", "names", "crates/alpha", [], [rule])
        ledger.ticket_assign("modules-1", "main")
        self.assertEqual(ledger.ticket_base("modules-1"), base)
        with self.assertRaisesRegex(style_ledger.LedgerError, f"^crates/alpha: not a file at the ticket's start {base[:7]}$"):
            self.record_removed(ledger, rule, "crates/alpha", base, ticket="modules-1")

    def test_removed_refuses_a_path_yielded_before_the_write_lock(self) -> None:
        ledger, rule, _ = self.removed_ticket()
        path = self.sources[1]
        (self.repo / path).unlink()
        deleting = self.commit("Ticket deletes the file")
        original = style_ledger._cell_scope  # pyright: ignore[reportPrivateUsage]

        def yield_then_scope(definition: rules.Rule, cell: str, tip: style_ledger.RepositoryTip) -> style_ledger.CellInputScope:
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("INSERT INTO ticket_yields VALUES (?,?,?)", ("names-1", path, "now"))
            return original(definition, cell, tip)

        with (mock.patch.object(style_ledger, "_cell_scope", side_effect=yield_then_scope),
              self.assertRaisesRegex(style_ledger.LedgerError, f"^names-1: {re.escape(path)} was yielded$")):
            self.record_removed(ledger, rule, path, deleting)
        self.assertEqual(self.cell_rows(ledger, path), [])

    def test_names_checkpoint_leaves_forbidden_words_judgments_stale_on_the_edited_path(self) -> None:
        names = self.rule("read")
        forbidden = self.rule("forbidden-words", "tier: C\nfamily: values\ninput_scope: file\n")
        path = self.sources[0]
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-1", "names", "read", "names", "crates/alpha", [path], [names, forbidden])
        ledger.ticket_assign("names-1", "main")
        self.assertEqual(ledger.ticket_base("names-1"), self.initial_tip)
        # Even a verdict recorded under this ticket is not its family's judgment.
        for rule in (names, forbidden):
            self.record(rule, path, self.initial_tip, ticket="names-1")
        self.write(path, "// A new comment needs its own word check.\npub fn renamed() {}\n")
        checkpoint = self.commit("Names checkpoint adds a comment")
        ledger.ticket_checkpoint("names-1", checkpoint)
        _ = ledger.ticket_merge("names-1")
        ledger.set_tip(checkpoint, "main")
        self.assertEqual(self.states(names)[path], "current")
        self.assertEqual(self.states(forbidden)[path], "stale")
        tickets = style_plan.candidate_tickets(
            self.repo, ledger, style_plan.BusyFiles(berth_board.Board(()), frozenset()), "values",
            base=style_plan.NoUnitBranch())
        self.assertTrue(any(path in ticket.files and forbidden in ticket.rules for ticket in tickets))

    def test_structure_checkpoint_requires_every_rule_and_accepts_any_verdict_at_head(self) -> None:
        first = self.rule("structure-first", "tier: D\nfamily: modules\n")
        missing = self.rule("structure-last", "tier: D\nfamily: modules\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha", [], [missing, first])
        ledger.ticket_assign("modules-unit-1", "main")
        _ = ledger.hold(first, "crates/alpha", 1, "Split crate", "restructure", "before", "after", self.blob(first), "modules-unit-1")
        self.record(first, "crates/alpha", self.initial_tip, "held", ticket="modules-unit-1")

        refused = self.cli("ticket", "checkpoint", "--id", "modules-unit-1", "--commit", "HEAD", success=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: modules-unit-1: unjudged cells: {missing} crates/alpha")
        self.assertNotIn(f"{first} crates/alpha", refused.stderr)
        self.assertEqual(ledger.ticket_open("modules-unit")[0]["state"], "assigned")

        self.record(missing, "crates/alpha", self.initial_tip, "waived", detail="accepted", ticket="modules-unit-1")
        for _ in range(2):
            _ = self.cli("ticket", "checkpoint", "--id", "modules-unit-1", "--commit", "HEAD")
        checkpointed, = ledger.ticket_open("modules-unit")
        self.assertEqual((checkpointed["state"], checkpointed["checkpoint"]), ("checkpointed", self.initial_tip))
        with mock.patch.object(ledger, "unjudged_ticket_cells", side_effect=AssertionError("rechecked an idempotent checkpoint")):
            ledger.ticket_checkpoint("modules-unit-1", self.initial_tip)
        _ = self.cli("ticket", "merge", "--id", "modules-unit-1")
        self.assertEqual(ledger.ticket_open("modules-unit"), ())

    def test_a_style_checkpoint_keeps_its_reservation_until_ticket_merge_releases_it(self) -> None:
        session = self.held_reservation("modules-unit-1")
        half = self.cli("ticket", "checkpoint", "--id", "modules-unit-1", "--commit", "HEAD", "--session-dir", str(session),
                        success=False)
        self.assertEqual(half.returncode, 2)
        self.assertIn("ticket checkpoint takes --session-dir and --phase together", half.stderr)
        self.checkpointed_structure_ticket("modules-unit-1", "--session-dir", str(session), "--phase", "7")
        self.assertEqual(self.berth_calls(), [])
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout,
                         "ticket modules-unit-1: phase 7: released reservation r1\n")
        self.assertIn(["release", "r1", "--json"], self.berth_calls())
        self.assertFalse((session / "phases/7/delegated_phase_reservation_state.json").exists())
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", "", ""))
        calls = self.berth_calls()
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout, "")
        self.assertEqual(self.berth_calls(), calls)

    def test_a_checkpoint_stores_its_reservation_in_the_same_write_as_its_state(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.assigned_structure_ticket("modules-unit-1")
        with closing(sqlite3.connect(style_ledger.ledger_location(self.repo).database)) as connection:
            _ = connection.execute("CREATE TRIGGER lost_reservation BEFORE UPDATE OF reservation_session_dir ON tickets "
                                   + "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
            connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "disk full"):
            self.ledger().ticket_checkpoint("modules-unit-1", "HEAD", str(session), "7")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("assigned", "", ""))
        with closing(sqlite3.connect(style_ledger.ledger_location(self.repo).database)) as connection:
            _ = connection.execute("DROP TRIGGER lost_reservation")
            connection.commit()
        self.ledger().ticket_checkpoint("modules-unit-1", "HEAD", str(session), "7")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("checkpointed", str(session), "7"))

    def test_a_repeat_checkpoint_stores_a_missing_reservation_and_refuses_another(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.checkpointed_structure_ticket("modules-unit-1")
        self.ledger().ticket_checkpoint("modules-unit-1", "HEAD", str(session), "7")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("checkpointed", str(session), "7"))
        with self.assertRaisesRegex(style_ledger.LedgerError, f"checkpointed holding the reservation of phase 7 in {re.escape(str(session))}"):
            self.ledger().ticket_checkpoint("modules-unit-1", "HEAD", str(session), "8")
        self.ledger().ticket_checkpoint("modules-unit-1", "HEAD")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("checkpointed", str(session), "7"))

    def test_a_checkpoint_that_names_no_reservation_record_merges_as_before(self) -> None:
        session = self.held_reservation("modules-unit-1")
        state = (session / "phases/7/delegated_phase_reservation_state.json").read_bytes()
        self.checkpointed_structure_ticket("modules-unit-1")
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout, "")
        self.assertEqual(self.berth_calls(), [])
        self.assertEqual((session / "phases/7/delegated_phase_reservation_state.json").read_bytes(), state)

    def test_a_reservation_ticket_merge_cannot_release_stays_held_until_a_rerun(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.checkpointed_structure_ticket("modules-unit-1", "--session-dir", str(session), "--phase", "7")
        self.write("README.md", "moved on\n")
        moved = self.commit("Move the unit worktree off its checkpoint")
        refused = self.cli("ticket", "merge", "--id", "modules-unit-1", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn(f"style_ledger: ticket modules-unit-1: phase_reservation: {self.repo} is at {moved[:7]}, not checkpoint "
                      + f"{self.initial_tip[:7]}; reservation r1 stays held; rerun ticket merge", refused.stderr)
        self.assertEqual(self.ledger().ticket_open("modules-unit"), ())
        self.assertNotIn("release", [call[0] for call in self.berth_calls()])
        # Only a merged ticket releases its reservation.
        self.checkpointed_structure_ticket("modules-unit-2", "--session-dir", str(session), "--phase", "7")
        with self.assertRaisesRegex(style_ledger.LedgerError, "released at ticket merge, not in state checkpointed"):
            _ = self.ledger().release_held_reservation("modules-unit-2")
        _ = self.git("reset", "-q", "--hard", self.initial_tip)
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout,
                         "ticket modules-unit-1: phase 7: released reservation r1\n")

    def test_reconcile_retries_a_merged_ticket_reservation_until_its_release_is_confirmed(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.checkpointed_structure_ticket("modules-unit-1", "--session-dir", str(session), "--phase", "7")
        self.write("README.md", "moved on\n")
        moved = self.commit("Move the unit worktree off its checkpoint")
        changes = self.ledger().reconcile("main", "main")
        self.assertIn("ticket modules-unit-1: checkpointed -> merged", changes)
        self.assertIn(f"ticket modules-unit-1: phase_reservation: {self.repo} is at {moved[:7]}, not checkpoint "
                      + f"{self.initial_tip[:7]}; reservation r1 stays held", changes)
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", str(session), "7"))
        _ = self.git("reset", "-q", "--hard", self.initial_tip)
        self.assertIn("ticket modules-unit-1: phase 7: released reservation r1", self.ledger().reconcile("main", "main"))
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", "", ""))
        calls = self.berth_calls()
        self.assertEqual(self.ledger().reconcile("main", "main"), ())
        self.assertEqual(self.berth_calls(), calls)

    def test_ticket_merge_takes_a_release_at_a_later_merge_once_the_style_branch_contains_it(self) -> None:
        session = self.held_reservation("modules-unit-1")
        self.checkpointed_structure_ticket("modules-unit-1", "--session-dir", str(session), "--phase", "7")
        # The unit merges style/main after its checkpoint and berth releases there (cargo-liner ticket 90).
        _ = self.git("checkout", "-q", "-b", "unit")
        self.write("README.md", "merged style/main\n")
        later = self.commit("Merge style/main into the unit")
        _ = (self.root / "board.json").write_text(json.dumps({"r1": {"status": "released_after_checkpoint", "protected_tip": later}}))
        refused = self.cli("ticket", "merge", "--id", "modules-unit-1", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn(f"or at a later commit that {self.initial_tip[:7]} contains; it stays held; rerun ticket merge", refused.stderr)
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", str(session), "7"))
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "-q", "--ff-only", "unit")
        self.assertEqual(self.cli("ticket", "merge", "--id", "modules-unit-1").stdout,
                         "ticket modules-unit-1: phase 7: released reservation r1\n")
        self.assertEqual(self.ticket_row("modules-unit-1"), ("merged", "", ""))
        self.assertEqual({call[0] for call in self.berth_calls()}, {"board"})

    def test_read_checkpoint_names_only_the_missing_rule_and_file_pair(self) -> None:
        first, last = self.rule("read-first"), self.rule("read-last")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [last, first])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        complete = ((first, self.sources[0]), (first, self.sources[1]), (last, self.sources[0]))
        for rule, path in complete:
            self.record(rule, path, self.initial_tip, ticket="read-1")

        refused = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", "HEAD", success=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(f"{last} {self.sources[1]}", refused.stderr)
        for rule, path in complete:
            self.assertNotIn(f"{rule} {path}", refused.stderr)
        self.assertEqual(self.ledger().ticket_open("read")[0]["state"], "assigned")
        self.assertEqual(self.ledger().unjudged_ticket_cells("read-1", "HEAD", self.in_worktree),
                         (style_ledger.UnjudgedTicketCell(last, self.sources[1]),))

    def test_judgments_without_the_owned_ticket_do_not_complete_its_cells(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        self.add_ticket("other-1", [rule], unit="other")
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        for ticket in ("", "other-1"):
            with self.subTest(judged_under=ticket):
                for path in self.sources[:2]:
                    self.record(rule, path, self.initial_tip, ticket=ticket)
                refused = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", "HEAD", success=False)
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertEqual(refused.stderr.strip(),
                                 f"style_ledger: read-1: unjudged cells: {rule} {self.sources[0]}, {rule} {self.sources[1]}")
                self.assertEqual(self.ledger().ticket_open("read")[0]["state"], "assigned")

    def test_listed_checkpoint_requires_a_verdict_for_each_file_of_its_rule(self) -> None:
        rule = self.rule("listed", "tier: B\nfamily: names\ninput_scope: file\npre_filter: value\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("listed-1", "listed", "listed", "names", "crates/alpha", list(self.sources[:2]), [rule], rule)
        ledger.ticket_assign("listed-1", "main")
        self.record(rule, self.sources[0], self.initial_tip, ticket="listed-1")
        refused = self.cli("ticket", "checkpoint", "--id", "listed-1", "--commit", "HEAD", success=False)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(f"{rule} {self.sources[1]}", refused.stderr)
        self.assertNotIn(f"{rule} {self.sources[0]}", refused.stderr)
        self.record(rule, self.sources[1], self.initial_tip, ticket="listed-1")
        _ = self.cli("ticket", "checkpoint", "--id", "listed-1", "--commit", "HEAD")

    def test_yielded_read_file_returns_to_open_and_does_not_block_checkpoint(self) -> None:
        first, last = self.rule("read-first"), self.rule("read-last")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [last, first])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        before = {rule: self.states(rule)[self.sources[0]] for rule in (first, last)}
        for rule in (first, last):
            for path in self.sources[:2]:
                self.record(rule, path, self.initial_tip, ticket="read-1")
        self.assertTrue(all(self.states(rule)[self.sources[0]] == "current" for rule in (first, last)))

        _ = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[0])

        self.assertEqual({rule: self.states(rule)[self.sources[0]] for rule in (first, last)}, before)
        self.assertEqual(self.ledger().unjudged_ticket_cells("read-1", "HEAD", self.in_worktree), ())
        _ = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", "HEAD")
        self.assertEqual(self.ledger().ticket_open("read")[0]["state"], "checkpointed")

    def test_yield_removes_only_its_own_verdicts_and_unfiled_findings_on_that_file(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        self.add_ticket("other-1", [rule], unit="other")
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        self.record(rule, self.sources[0], self.initial_tip, "waived", ticket="other-1")
        removed = self.hold_finding(rule, 1, ticket="read-1")
        self.record(rule, self.sources[0], self.initial_tip, "held", ticket="read-1")
        self.record(rule, self.sources[1], self.initial_tip, ticket="read-1")
        filed = self.hold_finding(rule, 2, ticket="read-1")
        waived = self.hold_finding(rule, 3, ticket="read-1")
        other = self.hold_finding(rule, 4, ticket="other-1")
        ledger = self.ledger()
        proposal = self.root / "issues/filed.md"
        ledger.file_held(filed, proposal)
        ledger.waive_held(waived, style_ledger.WaiveByPerson("nate"))
        untouched = ledger.hold(rule, self.sources[1], 5, "other file", "rename", "old", "new",
                                self.blob(rule), "read-1")

        _ = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[0])

        with closing(sqlite3.connect(ledger.database)) as connection:
            judgments = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT ticket,path,verdict FROM judgments ORDER BY ticket,path",
            ).fetchall())
        self.assertEqual(judgments, [("other-1", self.sources[0], "waived"), ("read-1", self.sources[1], "clean")])
        findings = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertNotIn(removed, findings)
        self.assertEqual(set(findings), {filed, waived, other, untouched.finding_id})
        self.assertEqual(findings[filed].state, style_ledger.Filed(proposal))
        self.assertIsInstance(findings[waived].state, style_ledger.Waived)
        self.assertIsInstance(findings[other].state, style_ledger.Unfiled)
        self.assertIsInstance(findings[untouched.finding_id].state, style_ledger.Unfiled)

    def test_yield_refuses_nonassigned_and_nonfile_tickets_and_outside_paths(self) -> None:
        rule = self.rule("read")
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        ledger = self.ledger()
        ledger.ticket_add("modules-1", "modules", "structure", "modules", "crates/alpha", [], [structure])
        ledger.ticket_assign("modules-1", "main")
        finding = self.hold_finding(rule, 1)
        issue = self.root / "issues/approved.md"
        ledger.file_held(finding, issue)
        ledger.ticket_add("proposal-1", "proposal", "proposal", "names", "crates/alpha",
                          [self.sources[0]], [rule], proposal=str(issue), finding_ids=(finding,))
        ledger.ticket_assign("proposal-1", "main")
        cases = (("read-1", self.sources[0], "cannot yield from queued"),
                 ("modules-1", self.sources[0], "cannot yield a structure ticket"),
                 ("proposal-1", self.sources[0], f"{self.sources[0]} holds every finding the ticket has left; "
                  + "drop the ticket with style_ledger.py ticket drop"))
        for ticket, path, reason in cases:
            with self.subTest(ticket=ticket):
                refused = self.cli("ticket", "yield", "--id", ticket, "--path", path, success=False)
                self.assertEqual(refused.returncode, 2)
                self.assertEqual(refused.stderr.strip(), f"style_ledger: {ticket}: {reason}")
        ledger.ticket_assign("read-1", "main")
        refused = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[2], success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: read-1: {self.sources[2]} is not in ticket files")
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, ticket="read-1")
        ledger.ticket_checkpoint("read-1", self.initial_tip)
        refused = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[0], success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.strip(), "style_ledger: read-1: cannot yield from checkpointed")
        self.assertEqual(self.states(rule)[self.sources[0]], "current")
        self.assertEqual(ledger.held_findings(style_ledger.AllHeldFindings())[0].state, style_ledger.Filed(issue))

    def test_a_proposal_yield_keeps_the_yielded_finding_filed_and_free_for_a_later_ticket(self) -> None:
        kept, freed, issue = self.yield_proposal((self.sources[3], "rename", "Outcome::Old", "Outcome::New"),
                                                 (self.sources[1], "rename", "Shape::Round", "Shape::Square"))
        ledger = self.ledger()
        refused = self.cli("ticket", "yield", "--id", "proposal-1", "--path", self.sources[0], success=False)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: proposal-1: no finding of the ticket is at {self.sources[0]}")
        yielded = self.cli("ticket", "yield", "--id", "proposal-1", "--path", self.sources[1])
        self.assertEqual(yielded.stdout, f"yielded {freed}: it stays filed for a later ticket\n")
        findings = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertEqual(findings[freed].state, style_ledger.Filed(issue))
        self.assertEqual(findings[freed].build_ticket, style_ledger.NoBuildTicket())
        self.assertEqual(findings[kept].build_ticket, style_ledger.BuildTicket("proposal-1"))
        ticket = ledger.ticket_open("proposal")[0]
        self.assertEqual(ticket["finding_ids"], [kept])
        self.assertEqual([path["path"] for path in ticket["yielded"]], [self.sources[1]])
        self.assertEqual(self.cli("ticket", "yield", "--id", "proposal-1", "--path", self.sources[1]).stdout, "")
        self.assertEqual(ledger.ticket_open("proposal")[0], ticket)
        refused = self.cli("ticket", "yield", "--id", "proposal-1", "--path", self.sources[3], success=False)
        self.assertIn("holds every finding the ticket has left", refused.stderr)
        # The merge builds the kept finding and leaves the yielded one filed for a later ticket.
        self.write(self.sources[3], "enum Outcome { New }\n")
        checkpoint = self.commit("Build the kept finding")
        ledger.set_tip(checkpoint, "main")
        ledger.ticket_checkpoint("proposal-1", checkpoint)
        self.assertEqual(ledger.ticket_merge("proposal-1"), ())
        findings = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
        self.assertEqual(findings[kept].state, style_ledger.Built(issue))
        self.assertEqual((findings[freed].state, findings[freed].build_ticket), (style_ledger.Filed(issue), style_ledger.NoBuildTicket()))
        ledger.ticket_add("proposal-2", "proposal", "proposal", "names", "crates/alpha", [self.sources[1]],
                          [findings[freed].rule], proposal=str(issue), finding_ids=(freed,))
        self.assertEqual(ledger.held_findings(style_ledger.AllHeldFindings())[1].build_ticket, style_ledger.BuildTicket("proposal-2"))

    def test_a_proposal_yield_frees_a_file_rename_beside_an_independent_one_in_its_crate(self) -> None:
        # Mounts inside alpha, also through an inline module's folder, and one from beta to a file of no finding's
        # crate, join no finding crate to another.
        mounts = {"crates/alpha/src/other.rs": self.yield_files["crates/alpha/src/other.rs"] + "#[path = \"c.rs\"]\nmod again;\n"
                  + "#[path = \".\"]\nmod folded {\n    #[path = \"c.rs\"]\n    mod again;\n}\n",
                  "crates/beta/src/other.rs": "#[path = \"../../../vendor/third-party/src/lib.rs\"]\nmod third;\n"}
        kept, freed, _ = self.yield_proposal(self.kept_rename, self.freed_rename, {**self.yield_files, **mounts})
        self.assertEqual(self.ledger().ticket_yield("proposal-1", self.freed_rename[0]), (freed,))
        self.assertEqual(self.ledger().ticket_open("proposal")[0]["finding_ids"], [kept])

    def test_a_proposal_yield_refuses_a_kept_finding_that_can_need_the_yielded_one(self) -> None:
        library = "crates/alpha/src/lib.rs"
        reexport = {library: "mod producer; mod consumer;\npub use producer::make;\n",
                    "crates/alpha/src/producer.rs": "pub fn make() -> u32 { 1 }\n",
                    "crates/alpha/src/consumer.rs": "pub fn take() -> u32 { crate::make() }\n"}
        qualified = ("crates/alpha/src/b.rs", "rename", "Shape::Round", "Shape::Square")
        cases: tuple[tuple[str, tuple[str, str, str, str], tuple[str, str, str, str], dict[str, str], str], ...] = (
            ("a re-export carries the yielded type", ("crates/alpha/src/consumer.rs", "type", "pub fn take() -> u32", "u64"),
             ("crates/alpha/src/producer.rs", "type", "pub fn make() -> u32", "u64"), reexport,
             "{kept} is not a file rename, and crates/alpha holds a freed and a kept finding"),
            ("a qualified rename beside a file rename", self.kept_rename, qualified, {},
             "{freed} is not a file rename, and crates/alpha holds a freed and a kept finding"),
            ("the yielded file names the kept old module", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/b.rs": "pub fn beta() { crate::a::alpha() }\n"},
             "crates/alpha/src/b.rs names a, a module that {kept} renames"),
            ("the yielded file names the kept new module", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/b.rs": "pub fn first() {}\n"}, "crates/alpha/src/b.rs names first, a module that {kept} renames"),
            ("one new name", self.kept_rename, (*self.freed_rename[:3], "Rename src/b.rs to src/first.rs"), {},
             "{freed} and {kept} both rename to or from first"),
            ("the yielded new name is the kept old name", self.kept_rename,
             (*self.freed_rename[:3], "Rename src/b.rs to src/a.rs"), {}, "{freed} and {kept} both rename to or from a"),
            ("the kept new name is the yielded old name", (*self.kept_rename[:3], "Rename src/a.rs to src/b.rs"),
             self.freed_rename, {}, "{freed} and {kept} both rename to or from b"),
            ("a public use of a renamed module", self.kept_rename, self.freed_rename,
             {library: "mod a; mod b; mod c; mod other;\npub use a::alpha;\n"}, f"{library} re-exports a"),
            ("a glob use of the renamed modules' parent", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/other.rs": "use super::*;\n"}, "crates/alpha/src/other.rs glob-imports crate"),
            ("a macro_rules item", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/c.rs": "macro_rules! made { () => {} }\n"}, "crates/alpha/src/c.rs holds macro_rules"),
            ("an include call", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/c.rs": "include!(\"made.rs\");\n"}, "crates/alpha/src/c.rs holds include"),
            ("a path attribute mounts the yielded file into the kept crate",
             ("crates/alpha/src/consumer.rs", "type", "pub fn take() -> u32", "u64"),
             ("crates/beta/src/producer.rs", "type", "pub fn make() -> u32", "u64"),
             {library: "#[path = \"../../beta/src/producer.rs\"]\nmod producer;\nmod consumer;\n"},
             f"{library} mounts crates/beta/src/producer.rs, a file outside crates/alpha"),
            ("a cfg_attr path attribute mounts the yielded file", ("crates/alpha/src/consumer.rs", "type", "u32", "u64"),
             ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {library: "#[cfg_attr(all(), cfg_attr(test, path = \"../../beta/src/producer.rs\"))]\nmod producer;\n"},
             f"{library} mounts crates/beta/src/producer.rs, a file outside crates/alpha"),
            ("another crate mounts the yielded file", self.kept_rename, self.freed_rename,
             {"crates/beta/src/lib.rs": "#[path = \"../../alpha/src/b.rs\"]\nmod b;\n"},
             "crates/beta/src/lib.rs mounts crates/alpha/src/b.rs, a file outside crates/beta"),
            ("a path attribute mounts a file that is not .rs", ("crates/alpha/src/consumer.rs", "type", "u32", "u64"),
             ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {library: "#[path = \"mount.inc\"]\nmod mount;\n",
              "crates/alpha/src/mount.inc": "#[path = \"../../beta/src/producer.rs\"]\nmod producer;\n"},
             f"{library} mounts crates/alpha/src/mount.inc, which is not a .rs file"),
            ("an inline module's folder is in another crate", ("crates/alpha/src/consumer.rs", "type", "u32", "u64"),
             ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {library: "#[path = \"../../beta/src\"]\nmod mounted {\n    mod producer;\n}\n"},
             f"{library} mounts crates/beta/src, a folder outside crates/alpha"),
            ("a path attribute in an inline module reads from the module's folder",
             ("crates/alpha/src/consumer.rs", "type", "u32", "u64"), ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {library: "#[path = \"a/b\"]\nmod up {\n    #[path = \"../../../../beta/src/producer.rs\"]\n    mod producer;\n}\n"},
             f"{library} mounts crates/beta/src/producer.rs, a file outside crates/alpha"),
            ("an inactive path attribute leaves the module's own folder a reading",
             ("crates/alpha/src/consumer.rs", "type", "u32", "u64"), ("crates/beta/src/producer.rs", "type", "u32", "u64"),
             {"Cargo.toml": '[workspace]\nmembers = ["crates/alpha", "crates/beta", "crates/gamma"]\nresolver = "2"\n',
              "crates/gamma/Cargo.toml": '[package]\nname = "gamma"\nversion = "0.1.0"\nedition = "2021"\n',
              "crates/gamma/src/lib.rs": "#[cfg_attr(any(), path = \".\")]\npub mod deep {\n"
              + "    #[path = \"../../../beta/src/producer.rs\"]\n    pub mod producer;\n}\n"},
             "crates/gamma/src/lib.rs mounts crates/beta/src/producer.rs, a file outside crates/gamma"),
            ("a path attribute leaves the repository", self.kept_rename, self.freed_rename,
             {library: "mod a; mod b; mod c; mod other;\n#[path = \"../../../../outside.rs\"]\nmod outside;\n"},
             f"{library} mounts ../outside.rs, a file outside crates/alpha"),
            ("a path attribute in a macro of another crate", self.kept_rename, self.freed_rename,
             {"crates/beta/src/other.rs": "macro_rules! mount { () => { #[path = \"x.rs\"] mod x; } }\n"},
             "crates/beta/src/other.rs holds a path attribute in a macro"),
            ("a path attribute that is not a string", self.kept_rename, self.freed_rename,
             {"crates/beta/src/other.rs": "#[path = concat!(\"x\", \".rs\")]\nmod x;\n"},
             "crates/beta/src/other.rs holds a path attribute that is not a relative string"),
            ("a file that does not tokenize", self.kept_rename, self.freed_rename,
             {"crates/alpha/src/c.rs": "pub fn odd() { \\ }\n"}, "crates/alpha/src/c.rs does not tokenize"),
        )
        for name, kept, freed, files, reason in cases:
            with self.subTest(name):
                self.setUp()
                kept_id, freed_id, _ = self.yield_proposal(kept, freed, {**self.yield_files, **files})
                ledger = self.ledger()
                with self.assertRaises(style_ledger.LedgerError) as refused:
                    _ = ledger.ticket_yield("proposal-1", freed[0])
                self.assertIn(f"proposal-1: a kept finding can need a finding at {freed[0]}", str(refused.exception))
                self.assertIn(reason.format(kept=kept_id, freed=freed_id), str(refused.exception))
                ticket = ledger.ticket_open("proposal")[0]
                self.assertEqual((ticket["finding_ids"], ticket["yielded"]), (sorted([kept_id, freed_id]), ()))

    def test_a_proposal_yield_reads_again_when_the_synced_tip_moves_before_its_lock(self) -> None:
        _ = self.yield_proposal(self.kept_rename, self.freed_rename, self.yield_files)
        self.write("crates/alpha/src/c.rs", "macro_rules! made { () => {} }\n")
        moved = self.commit("Add a macro")
        ledger = self.ledger()
        read = style_ledger.Ledger._yielded_findings  # pyright: ignore[reportPrivateUsage]
        calls: list[str] = []

        def advancing(self: style_ledger.Ledger, ticket: style_ledger.TicketRecord, findings: tuple[style_ledger.HeldFinding, ...],
                      path: str) -> style_ledger.FileYield | style_ledger.FindingYield:
            calls.append(self.synced_commit())
            result = read(self, ticket, findings, path)
            if len(calls) == 1:
                self.set_tip(moved)
            return result

        with mock.patch.object(style_ledger.Ledger, "_yielded_findings", advancing), \
                self.assertRaises(style_ledger.LedgerError) as refused:
            _ = ledger.ticket_yield("proposal-1", self.freed_rename[0])
        self.assertIn("crates/alpha/src/c.rs holds macro_rules", str(refused.exception))
        self.assertEqual(calls, [self.git("rev-parse", "HEAD~1"), moved])
        self.assertEqual(ledger.ticket_open("proposal")[0]["yielded"], ())

    def test_repeated_listed_yield_preserves_the_recorded_yield_and_remaining_ownership(self) -> None:
        rule = self.rule("listed", "tier: B\nfamily: names\ninput_scope: file\npre_filter: value\n")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("listed-1", "listed", "listed", "names", "crates/alpha", list(self.sources[:2]), [rule], rule)
        ledger.ticket_assign("listed-1", "main")
        self.record(rule, self.sources[0], self.initial_tip, ticket="listed-1")
        _ = self.cli("ticket", "yield", "--id", "listed-1", "--path", self.sources[0])
        with closing(sqlite3.connect(ledger.database)) as connection:
            before = cast(list[tuple[str, str, str]], connection.execute("SELECT * FROM ticket_yields").fetchall())
        self.assertEqual([(ticket, path) for ticket, path, _ in before], [("listed-1", self.sources[0])])
        _ = self.cli("ticket", "yield", "--id", "listed-1", "--path", self.sources[0])
        with closing(sqlite3.connect(ledger.database)) as connection:
            after = cast(list[tuple[str, str, str]], connection.execute("SELECT * FROM ticket_yields").fetchall())
        self.assertEqual(after, before)
        self.assertEqual(ledger.unjudged_ticket_cells("listed-1", "HEAD", self.in_worktree), (style_ledger.UnjudgedTicketCell(rule, self.sources[1]),))

    def test_record_under_a_yielded_ticket_is_refused_without_recreating_its_verdict(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        _ = self.cli("ticket", "yield", "--id", "read-1", "--path", self.sources[0])
        refused = self.cli("record", "--rule", rule, "--path", self.sources[0], "--commit", self.initial_tip,
                           "--rule-blob", self.blob(rule), "--verdict", "clean", "--ticket", "read-1",
                           "--worker", "test-worker", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.strip(), f"style_ledger: read-1: {self.sources[0]} was yielded")
        self.assertEqual(self.states(rule)[self.sources[0]], "open")
        self.record(rule, self.sources[0], self.initial_tip, ticket="another-ticket")
        self.assertEqual(self.states(rule)[self.sources[0]], "current")

    def test_ticket_resume_transitions_repeats_and_rejected_skips(self) -> None:
        rule_id = self.rule("read")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "main")
        self.add_ticket("read-1", [rule_id], unit="read")
        self.add_ticket("read-1", [rule_id], unit="read")
        self.assertEqual(self.cli("ticket", "open", "--unit", "read").stdout.strip(), "")
        skipped = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", self.initial_tip, success=False)
        self.assertEqual(skipped.returncode, 2)
        self.assertIn("queued", skipped.stderr)
        for _ in range(2):
            _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "sample-style-read")
        self.assertIn("read-1", self.cli("ticket", "open", "--unit", "read").stdout)
        for path in self.sources[:2]:
            self.record(rule_id, path, self.initial_tip, ticket="read-1")
        for _ in range(2):
            _ = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", self.initial_tip)
        self.assertIn("read-1", self.cli("ticket", "open", "--unit", "read").stdout)
        backward = self.cli("ticket", "assign", "--id", "read-1", "--branch", "sample-style-read", success=False)
        self.assertEqual(backward.returncode, 2)
        self.assertIn("checkpointed", backward.stderr)
        for _ in range(2):
            _ = self.cli("ticket", "merge", "--id", "read-1")
        self.assertEqual(self.cli("ticket", "open", "--unit", "read").stdout.strip(), "")
        dropped_final = self.cli("ticket", "drop", "--id", "read-1", success=False)
        self.assertEqual(dropped_final.returncode, 2)
        self.assertIn("merged", dropped_final.stderr)

    def test_ensuring_the_exact_tip_and_style_branch_never_calls_set_tip(self) -> None:
        _ = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "style/main")
        before = ledger.synced_at()

        with mock.patch.object(ledger, "set_tip", side_effect=AssertionError("rewrote an exact sync match")):
            ledger.ensure_synced_tip(self.initial_tip, "style/main")
            ledger.ensure_synced_tip(self.initial_tip, "refs/heads/style/main")

        self.assertEqual(ledger.summary().synced_tip, self.initial_tip)
        self.assertEqual(ledger.synced_at(), before)

    def test_reconcile_new_style_branch_syncs_at_its_commit_before_the_branch_point(self) -> None:
        _ = self.rule("read")
        self.write("crates/beta/src/style.rs", "pub fn style_input() {}\n")
        style_tip = self.commit("Create the style branch input")
        _ = self.git("branch", "style/main")
        self.write("crates/beta/src/new.rs", "pub fn new_input() {}\n")
        branch_tip = self.commit("Advance branch point before style reconciliation")
        ledger = self.ledger()
        for previous_branch in ("", "style/previous"):
            with self.subTest(previous_branch=previous_branch):
                if previous_branch:
                    ledger.set_tip(self.initial_tip, previous_branch)
                else:
                    ledger.set_tip(self.initial_tip)

                changes = ledger.reconcile("main", "style/main")

                self.assertEqual(changes, ("style branch refs/heads/style/main",))
                self.assertEqual(ledger.summary().synced_tip, style_tip)
                paths = {cell.path for cell in ledger.cells()}
                self.assertIn("crates/beta/src/style.rs", paths)
                self.assertNotIn("crates/beta/src/new.rs", paths)
                self.assertEqual(self.git("rev-parse", "refs/heads/style/main"), style_tip)
                self.assertEqual(self.git("rev-parse", "main"), branch_tip)
                with closing(sqlite3.connect(ledger.database)) as connection:
                    stored = cast(tuple[str, str], connection.execute("SELECT tip,style_branch FROM sync").fetchone())
                self.assertEqual(stored, (style_tip, "refs/heads/style/main"))

    def test_reconcile_missing_style_branch_uses_the_branch_point_commit(self) -> None:
        _ = self.rule("read")
        self.write("crates/beta/src/new.rs", "pub fn new_input() {}\n")
        branch_tip = self.commit("Advance branch point without a style branch")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "style/previous")

        changes = ledger.reconcile("main", "style/main")

        self.assertEqual(changes, ("style branch refs/heads/style/main",))
        self.assertEqual(ledger.summary().synced_tip, branch_tip)
        self.assertIn("crates/beta/src/new.rs", {cell.path for cell in ledger.cells()})
        with closing(sqlite3.connect(ledger.database)) as connection:
            stored = cast(tuple[str, str], connection.execute("SELECT tip,style_branch FROM sync").fetchone())
        self.assertEqual(stored, (branch_tip, "refs/heads/style/main"))
        self.assertNotIn("refs/heads/style/main", self.git("for-each-ref", "--format=%(refname)"))

    def test_reconcile_uses_the_pushed_tip_when_the_style_branch_is_missing(self) -> None:
        _ = self.rule("read")
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        self.write(self.sources[0], "pub fn unpushed() {}\n")
        _ = self.commit("Unpushed branch-point work")
        ledger = self.ledger()

        _ = ledger.reconcile("main", "style/main")

        self.assertEqual(ledger.summary().synced_tip, self.initial_tip)

    def test_origin_without_a_tracking_ref_refuses_to_use_the_local_branch_point(self) -> None:
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        with self.assertRaisesRegex(style_ledger.LedgerError, "origin.*refs/remotes/origin/main.*fetch"):
            _ = self.ledger().reconcile("main", "style/main")

    def test_reconcile_matching_style_branch_preserves_an_older_synced_tip(self) -> None:
        _ = self.rule("read")
        _ = self.git("branch", "style/main")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "style/main")
        before = ledger.synced_at()
        self.write("crates/beta/src/new.rs", "pub fn new_input() {}\n")
        _ = self.commit("Advance branch point after the existing sync")

        self.assertEqual(ledger.reconcile("main", "refs/heads/style/main"), ())

        self.assertEqual(ledger.summary().synced_tip, self.initial_tip)
        self.assertEqual(ledger.synced_at(), before)
        self.assertNotIn("crates/beta/src/new.rs", {cell.path for cell in ledger.cells()})

    def test_assigned_ticket_resumes_until_its_recorded_branch_is_deleted(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        _ = self.git("branch", "style/main")
        _ = self.git("branch", "sample-style-names")
        self.add_ticket("names-unit-1", [rule_id], unit="names-unit")
        missing = self.cli("ticket", "assign", "--id", "names-unit-1", success=False)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--branch", missing.stderr)
        self.assertEqual(self.cli("ticket", "open", "--unit", "names-unit").stdout.strip(), "")

        _ = self.cli("ticket", "assign", "--id", "names-unit-1", "--branch", "sample-style-names")
        opened = self.cli("ticket", "open", "--unit", "names-unit").stdout
        ticket = cast(style_ledger.TicketRecord, json.loads(opened))
        self.assertEqual(ticket["branch"], "refs/heads/sample-style-names")
        _ = self.cli("ticket", "assign", "--id", "names-unit-1", "--branch", "refs/heads/sample-style-names")
        self.assertEqual(self.cli("ticket", "open", "--unit", "names-unit").stdout, opened)
        refused = self.cli("ticket", "assign", "--id", "names-unit-1", "--branch", "other-branch", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn(ticket["branch"], refused.stderr)

        alive = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        self.assertNotIn("names-unit-1", alive.stdout)
        self.assertEqual(self.cli("ticket", "open", "--unit", "names-unit").stdout, opened)
        _ = self.git("branch", "-D", "sample-style-names")
        deleted = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        self.assertIn("ticket names-unit-1: assigned -> dropped", deleted.stdout)
        self.assertEqual(self.cli("ticket", "open", "--unit", "names-unit").stdout.strip(), "")
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            stored = cast(tuple[str, str], connection.execute(
                "SELECT state,branch FROM tickets WHERE id='names-unit-1'",
            ).fetchone())
        self.assertEqual(stored, ("dropped", ticket["branch"]))

    def test_ticket_drop_from_each_nonfinal_state_and_repeat(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        for index, state in enumerate(("queued", "assigned", "checkpointed"), 1):
            with self.subTest(state=state):
                unit = f"reader{index}"
                ticket = f"{unit}-1"
                self.add_ticket(ticket, [rule_id], unit=unit)
                if state != "queued":
                    _ = self.cli("ticket", "assign", "--id", ticket, "--branch", f"sample-style-{unit}")
                if state == "checkpointed":
                    for path in self.sources[:2]:
                        self.record(rule_id, path, self.initial_tip, ticket=ticket)
                    _ = self.cli("ticket", "checkpoint", "--id", ticket, "--commit", self.initial_tip)
                for _ in range(2):
                    _ = self.cli("ticket", "drop", "--id", ticket)
                refused = self.cli("ticket", "assign", "--id", ticket, "--branch", f"sample-style-{unit}", success=False)
                self.assertEqual(refused.returncode, 2)
                self.assertIn("dropped", refused.stderr)
                self.assertEqual(self.cli("ticket", "open", "--unit", unit).stdout.strip(), "")

    def test_ticket_pins_rule_text_and_refuses_a_mismatched_judgment(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        old_blob = self.blob(rule_id)
        self.add_ticket("read-1", [rule_id])
        rule_path = self.rule_dir / "read.md"
        _ = rule_path.write_text(rule_path.read_text() + "New requirement\n")
        new_blob = self.blob(rule_id)
        self.add_ticket("read-1", [rule_id])

        self.record(rule_id, self.sources[0], self.initial_tip, rule_blob=old_blob, ticket="read-1")
        _ = self.cli(
            "record", "--rule", rule_id, "--path", self.sources[1], "--commit", self.initial_tip,
            "--verdict", "clean", "--ticket", "read-1", "--worker", "test",
        )

        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")
        self.assertEqual(self.states(rule_id)[self.sources[1]], "stale")
        refused = self.cli(
            "record", "--rule", rule_id, "--path", self.sources[0], "--commit", self.initial_tip,
            "--rule-blob", new_blob, "--verdict", "clean", "--ticket", "read-1", "--worker", "test",
            success=False,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn(rule_id, refused.stderr)
        self.assertEqual(self.states(rule_id)[self.sources[0]], "stale")
        unpinned = self.cli(
            "record", "--rule", rule_id, "--path", self.sources[2], "--commit", self.initial_tip,
            "--verdict", "clean", "--ticket", "external", "--worker", "test", success=False,
        )
        self.assertEqual(unpinned.returncode, 2)
        self.assertEqual(self.states(rule_id)[self.sources[2]], "open")
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            pins = cast(list[tuple[str]], connection.execute("SELECT rule_blobs FROM tickets WHERE id='read-1'").fetchall())
        self.assertEqual(cast(dict[str, str], json.loads(pins[0][0])), {rule_id: old_blob})

    def test_listed_and_structure_tickets_keep_their_payloads_and_one_resume_point(self) -> None:
        listed = self.rule(
            "listed", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: value\n  surface: whole\n",
        )
        structure = self.rule("structure", "tier: D\nfamily: modules\n")
        self.set_tip(self.initial_tip)
        sites = [{"path": self.sources[0], "line": 1, "text": "pub fn value() -> u32 { 1 }"}]
        _ = self.cli(
            "ticket", "add", "--id", "listed-1", "--unit", "listed", "--kind", "listed",
            "--family", "names", "--crate", "crates/alpha", "--files", json.dumps([self.sources[0]]),
            "--rules", json.dumps([listed]), "--rule", listed, "--sites", json.dumps(sites),
        )
        _ = self.cli("ticket", "assign", "--id", "listed-1", "--branch", "sample-style-listed")
        self.record(listed, self.sources[0], self.initial_tip, ticket="listed-1")
        self.assertEqual(self.states(listed)[self.sources[0]], "current")
        self.add_ticket("listed-2", [structure], kind="structure", unit="listed")
        refused = self.cli("ticket", "assign", "--id", "listed-2", "--branch", "sample-style-structure", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("listed-1", refused.stderr)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            stored = cast(list[tuple[str, str, str, str]], connection.execute(
                "SELECT kind,rule,files,sites FROM tickets WHERE id='listed-1'",
            ).fetchall())[0]
        self.assertEqual(stored[:2], ("listed", listed))
        self.assertEqual(cast(list[str], json.loads(stored[2])), [self.sources[0]])
        self.assertEqual(cast(list[dict[str, object]], json.loads(stored[3])), sites)
        _ = self.cli("ticket", "drop", "--id", "listed-1")
        _ = self.cli("ticket", "assign", "--id", "listed-2", "--branch", "sample-style-structure")
        self.assertIn("listed-2", self.cli("ticket", "open", "--unit", "listed").stdout)

    def test_ticket_pins_uncovered_parent_and_refuses_unknown_or_unclassified_ids(self) -> None:
        partial = self.rule(
            "partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
            + "  kind: regex\n  pattern: absent_marker\n  surface: partial\n  uncovered: other names\n",
        )
        legacy = self.rule("legacy", "mechanism: llm\n")
        self.set_tip(self.initial_tip)
        uncovered = partial + "#uncovered"
        self.add_ticket("read-1", [uncovered])
        self.record(uncovered, self.sources[0], self.initial_tip, ticket="read-1")
        self.assertEqual(self.states(uncovered)[self.sources[0]], "current")
        for invalid in (legacy, "global:rust/missing.md"):
            with self.subTest(rule=invalid):
                refused = self.cli(
                    "ticket", "add", "--id", "read-2", "--unit", "read", "--kind", "read",
                    "--family", "names", "--crate", "crates/alpha", "--files", json.dumps([self.sources[0]]),
                    "--rules", json.dumps([invalid]), success=False,
                )
                self.assertEqual(refused.returncode, 2)
                self.assertIn(invalid, refused.stderr)

    def test_processed_tips_survive_reopen_without_marking_seen_as_done(self) -> None:
        self.set_tip(self.initial_tip)
        for _ in range(2):
            self.assertEqual(self.cli("tip", "seen", "--tip", self.initial_tip).stdout.strip(), "new")
        for _ in range(2):
            _ = self.cli("tip", "done", "--tip", self.initial_tip)
        self.assertEqual(self.cli("tip", "seen", "--tip", self.initial_tip).stdout.strip(), "seen")
        _ = self.git("commit", "--allow-empty", "-q", "-m", "New tip with same tree")
        self.assertEqual(self.cli("tip", "seen", "--tip", "HEAD").stdout.strip(), "new")

    def test_migration_23_preserves_processed_tips_as_linted(self) -> None:
        database = self.root / "legacy.sqlite"
        with closing(sqlite3.connect(database)) as connection, connection:
            for number, migration in sorted(style_ledger.MIGRATIONS.items()):
                if number < 23:
                    _ = connection.executescript(migration)
            _ = connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
            _ = connection.execute("INSERT INTO schema_version VALUES (22)")
            _ = connection.execute("INSERT INTO processed_tips VALUES (?,?)", (self.initial_tip, "2026-10-27"))
        with style_ledger.Ledger(self.repo, database) as ledger:
            self.assertEqual(ledger.tip_seen(self.initial_tip), "linted")
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM processed_tips").fetchone(),
                             (self.initial_tip, "2026-10-27", "linted"))

    def test_handoffs_require_ancestry_and_keep_sent_items_pending(self) -> None:
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checkpoint = self.commit("Style checkpoint")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        for _ in range(2):
            _ = self.cli("handoff", "add", "--checkpoint", checkpoint)
        refused = self.cli("handoff", "merged", "--checkpoint", checkpoint, success=False)
        self.assertEqual(refused.returncode, 2)
        for _ in range(2):
            _ = self.cli("handoff", "send", "--checkpoint", checkpoint, "--to", "parent")
        self.assertIn(checkpoint, self.cli("handoff", "pending").stdout)
        self.assertEqual(self.summary_report()["waiting_to_merge_back"], 1)
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate style")
        merge_commit = self.git("rev-parse", "HEAD")
        for _ in range(2):
            _ = self.cli("handoff", "merged", "--checkpoint", checkpoint, "--merge-commit", merge_commit)
        self.assertEqual(self.cli("handoff", "pending").stdout.strip(), "no pending hand-offs")
        backward = self.cli("handoff", "send", "--checkpoint", checkpoint, "--to", "parent", success=False)
        self.assertEqual(backward.returncode, 2)
        self.assertIn("merged", backward.stderr)

    def test_handoff_merged_takes_a_pending_row_once_the_branch_point_contains_it(self) -> None:
        """A queued notice left the row pending and its merge was refused (cargo-liner, 2026-10-27)."""
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checkpoint = self.commit("Style checkpoint")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        _ = self.cli("handoff", "add", "--checkpoint", checkpoint)
        refused = self.cli("handoff", "merged", "--checkpoint", checkpoint, success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("hand-off is not merged into main", refused.stderr)
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate style")
        merge_commit = self.git("rev-parse", "HEAD")
        _ = self.cli("handoff", "merged", "--checkpoint", checkpoint)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            stored = cast(tuple[str, str, str], connection.execute(
                "SELECT state,merge_commit,sent_to FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchone())
        self.assertEqual(stored, ("merged", merge_commit, ""))

    def test_handoff_merged_preserves_supplied_later_merge_evidence(self) -> None:
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn first_fix() {}\n")
        checkpoint = self.commit("First style checkpoint")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        _ = self.cli("handoff", "add", "--checkpoint", checkpoint)
        _ = self.cli("handoff", "send", "--checkpoint", checkpoint, "--to", "parent")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate first checkpoint")
        first_merge = self.git("rev-parse", "HEAD")
        _ = self.git("checkout", "-q", "style/main")
        self.write(self.sources[2], "pub fn second_fix() {}\n")
        _ = self.commit("Second style checkpoint")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate second checkpoint")
        second_merge = self.git("rev-parse", "HEAD")

        _ = self.cli("handoff", "merged", "--checkpoint", checkpoint, "--merge-commit", second_merge)

        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            stored = cast(tuple[str, str], connection.execute(
                "SELECT state,merge_commit FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchone())
        self.assertNotEqual(first_merge, second_merge)
        self.assertEqual(stored, ("merged", second_merge))

    def test_reconcile_and_handoff_merged_accept_only_a_pushed_branch_point_merge(self) -> None:
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checkpoint = self.commit("Style checkpoint")
        ledger = self.ledger()
        _ = ledger.reconcile("refs/heads/main", "style/main")
        ledger.handoff_add(checkpoint)
        ledger.handoff_send(checkpoint, "parent")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-qm", "Unpushed integration", "style/main")
        merge_commit = self.git("rev-parse", "HEAD")

        self.assertEqual(ledger.reconcile("refs/heads/main", "style/main"), ())
        self.assertEqual(ledger.pending_handoffs()[0]["state"], "sent")
        for evidence in ("", merge_commit):
            with self.subTest(evidence=evidence):
                with self.assertRaisesRegex(style_ledger.LedgerError, "hand-off is not merged"):
                    ledger.handoff_merged(checkpoint, evidence)
        _ = self.git("push", "-q", "origin", "main")
        ledger.handoff_merged(checkpoint)

        self.assertEqual(ledger.pending_handoffs(), ())
        with closing(sqlite3.connect(ledger.database)) as connection:
            stored = cast(tuple[str], connection.execute(
                "SELECT merge_commit FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchone())
        self.assertEqual(stored, (merge_commit,))

    def test_reconcile_fast_forward_evidence_stops_at_the_pushed_tip(self) -> None:
        origin = self.root / "origin.git"
        _ = self.git("init", "-q", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "main")
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn corrected() {}\n")
        checkpoint = self.commit("Style checkpoint")
        ledger = self.ledger()
        _ = ledger.reconcile("main", "style/main")
        ledger.handoff_add(checkpoint)
        ledger.handoff_send(checkpoint, "parent")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--ff-only", "-q", "style/main")
        _ = self.git("push", "-q", "origin", "main")
        self.write("README.md", "Unpushed product work.\n")
        _ = self.commit("Advance only local branch point")

        self.assertEqual(ledger.reconcile("main", "style/main"), (f"hand-off {checkpoint}: sent -> merged",))

        with closing(sqlite3.connect(ledger.database)) as connection:
            stored = cast(tuple[str], connection.execute(
                "SELECT merge_commit FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchone())
        self.assertEqual(stored, (checkpoint,))

    def test_reconcile_recovers_merged_handoff_ticket_and_missing_unit_branch(self) -> None:
        rule_id = self.rule("read")
        self.set_tip(self.initial_tip)
        _ = self.git("checkout", "-q", "-b", "style/main")
        self.write(self.sources[0], "pub fn first_fix() {}\n")
        merged_checkpoint = self.commit("First style checkpoint")
        self.add_ticket("worker-1", [rule_id], unit="worker")
        _ = self.git("branch", "sample-style-worker", merged_checkpoint)
        _ = self.cli("ticket", "assign", "--id", "worker-1", "--branch", "sample-style-worker")
        for path in self.sources[:2]:
            self.record(rule_id, path, merged_checkpoint, ticket="worker-1")
        _ = self.cli("ticket", "checkpoint", "--id", "worker-1", "--commit", merged_checkpoint)
        self.add_ticket("vanished-1", [rule_id], unit="vanished")
        _ = self.cli("ticket", "assign", "--id", "vanished-1", "--branch", "sample-style-vanished")
        _ = self.git("branch", "sample-style-live", merged_checkpoint)
        self.add_ticket("live-1", [rule_id], unit="live")
        _ = self.cli("ticket", "assign", "--id", "live-1", "--branch", "sample-style-live")
        _ = self.cli("handoff", "add", "--checkpoint", merged_checkpoint)
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-q", "style/main", "-m", "Integrate first checkpoint")
        first_merge = self.git("rev-parse", "HEAD")
        _ = self.git("checkout", "-q", "style/main")
        self.write(self.sources[2], "pub fn second_fix() {}\n")
        unmerged_checkpoint = self.commit("Second style checkpoint")
        _ = self.cli("handoff", "add", "--checkpoint", unmerged_checkpoint)

        result = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")

        self.assertIn(merged_checkpoint, result.stdout)
        self.assertIn("worker-1", result.stdout)
        self.assertIn("vanished-1", result.stdout)
        self.assertIn(unmerged_checkpoint, self.cli("handoff", "pending").stdout)
        self.assertNotIn(merged_checkpoint, self.cli("handoff", "pending").stdout)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            tickets = cast(list[tuple[str, str]], connection.execute("SELECT id,state FROM tickets ORDER BY id").fetchall())
            handoffs = cast(list[tuple[str, str, str]], connection.execute(
                "SELECT checkpoint,state,merge_commit FROM handoffs WHERE checkpoint=?", (merged_checkpoint,),
            ).fetchall())
            sync = cast(list[tuple[str, str]], connection.execute("SELECT tip,style_branch FROM sync").fetchall())
        self.assertEqual(tickets, [("live-1", "assigned"), ("vanished-1", "dropped"), ("worker-1", "merged")])
        self.assertEqual(handoffs, [(merged_checkpoint, "merged", first_merge)])
        self.assertEqual(sync, [(unmerged_checkpoint, "refs/heads/style/main")])
        self.assertEqual(self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main").stdout.strip(), "")

    def test_reconcile_bootstraps_an_unsynced_ledger_and_preserves_stored_tip(self) -> None:
        _ = self.rule("read")
        _ = self.git("branch", "style/main")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        report = self.summary_report()
        self.assertEqual(report["synced_tip"], self.initial_tip)
        self.write(self.sources[0], "pub fn product_change() {}\n")
        _ = self.commit("Advance branch point")
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "style/main")
        self.assertEqual(self.summary_report()["synced_tip"], self.initial_tip)

    def test_rule_edit_during_pinning_rolls_back_ticket_and_all_its_texts(self) -> None:
        first = self.rule("first")
        second = self.rule("second")
        ledger = self.ledger()
        catalog = rules.load_rules(self.repo)

        def catalog_then_edit(repo: Path) -> tuple[rules.Rule, ...]:
            self.assertEqual(repo, self.repo)
            path = self.rule_dir / "second.md"
            _ = path.write_text(path.read_text() + "Concurrent edit\n")
            return catalog

        with mock.patch.object(rules, "load_rules", side_effect=catalog_then_edit) as load:
            with self.assertRaisesRegex(style_ledger.ChangedBeforeLock, second + ": rule changed while pinning; rerun"):
                ledger.ticket_add("read-1", "read", "read", "names", "crates/alpha", list(self.sources[:2]), [first, second])
        self.assertEqual(load.call_count, style_ledger.LOCK_ATTEMPTS)
        self.assertEqual(ledger.queued_tickets("read"), ())
        with closing(sqlite3.connect(ledger.database)) as connection:
            texts = cast(list[tuple[str]], connection.execute("SELECT blob FROM rule_texts").fetchall())
        self.assertEqual(texts, [])

    def test_rule_edit_during_one_pinning_attempt_pins_the_edited_text(self) -> None:
        rule = self.rule("first")
        ledger = self.ledger()
        path = self.rule_dir / "first.md"
        catalog = rules.load_rules(self.repo)
        original = rules.load_rules

        def edit_once(repo: Path | None) -> tuple[rules.Rule, ...]:
            if repo == self.repo and not path.read_text().endswith("Concurrent edit\n"):
                _ = path.write_text(path.read_text() + "Concurrent edit\n")
                return catalog
            return original(repo)

        with mock.patch.object(rules, "load_rules", side_effect=edit_once) as load:
            ledger.ticket_add("read-1", "read", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        self.assertEqual(load.call_count, 2)
        pinned, = ledger.pinned_rule_texts("read-1")
        self.assertEqual(pinned.text, path.read_bytes())

    def test_ticket_start_is_recorded_once_and_survives_reopening_and_branch_deletion(self) -> None:
        rule = self.rule("read")
        _ = self.git("branch", "demo-style-names")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                              list(self.sources[:2]), [rule])
            with self.assertRaisesRegex(style_ledger.LedgerError, "cannot record start commit in state queued"):
                _ = ledger.ticket_base("names-unit-1")
            ledger.ticket_assign("names-unit-1", "demo-style-names")
            self.assertEqual(ledger.ticket_base("names-unit-1"), self.initial_tip)
            self.assertEqual(ledger.ticket_origin("names-unit-1"), style_ledger.SelectedByCoverage(self.initial_tip))

        self.write(self.sources[0], "pub fn changed() {}\n")
        advanced = self.commit("Advance unit branch")
        _ = self.git("branch", "-f", "demo-style-names", advanced)
        with style_ledger.open_ledger(self.repo) as ledger:
            self.assertEqual(ledger.ticket_base("names-unit-1"), self.initial_tip)
            self.assertEqual(ledger.ticket_origin("names-unit-1"), style_ledger.SelectedByCoverage(self.initial_tip))
            self.assertEqual(ledger.ticket_open("names-unit")[0]["base"], self.initial_tip)
            ledger.ticket_drop("names-unit-1")
        _ = self.git("branch", "-D", "demo-style-names")
        with style_ledger.open_ledger(self.repo) as ledger:
            self.assertEqual(ledger.ticket_base("names-unit-1"), self.initial_tip)
        self.assertEqual(self.git("for-each-ref", "refs/style-tickets"), "")

    def test_unit_rename_moves_current_rows_and_keeps_ticket_ids_and_history(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        files = list(self.sources[:2])
        for number in (8, 9, 1, 2):
            ledger.ticket_add(f"names-unit-{number}", "names-unit", "read", "names", "crates/alpha", files, [rule])
        ledger.ticket_assign("names-unit-8", "main")
        for path in files:
            self.record(rule, path, self.initial_tip, ticket="names-unit-8")
        ledger.ticket_checkpoint("names-unit-8", self.initial_tip)
        _ = ledger.ticket_merge("names-unit-8")
        ledger.ticket_drop("names-unit-9")
        ledger.ticket_assign("names-unit-2", "main")
        _ = ledger.hold(rule, files[0], 1, "pub fn old() {}", "rename", "old", "new",
                        self.blob(rule), "names-unit-2")
        ledger.record_candidate("names-unit-2", files[0], 1, "Check names in public APIs.")
        _ = ledger.ticket_yield("names-unit-2", files[1])
        ledger.wait_for_sync("names-unit")
        ledger.wait_for_sync("types-unit")
        tables = ("tickets", "sync_waits", "rule_candidates", "held_findings", "ticket_yields")
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("UPDATE held_findings SET build_ticket='names-unit-2'")
            connection.commit()
            before = {table: cast(list[tuple[object, ...]], connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid").fetchall()) for table in tables}
        new = "sample-style-names-unit"

        self.assertEqual(ledger.rename_unit("names-unit", new), style_ledger.UnitRenamed(2, 1))

        with closing(sqlite3.connect(ledger.database)) as connection:
            after = {table: cast(list[tuple[object, ...]], connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid").fetchall()) for table in tables}
        expected = dict(before)
        expected["tickets"] = [
            (row[0], new, *row[2:]) if row[1] == "names-unit" and row[10] in ("queued", "assigned", "checkpointed") else row
            for row in before["tickets"]
        ]
        expected["sync_waits"] = [(new, *row[1:]) if row[0] == "names-unit" else row
                                  for row in before["sync_waits"]]
        self.assertEqual(after, expected)
        self.assertEqual([ticket["id"] for ticket in self.ledger().queued_tickets(new)], ["names-unit-1"])
        self.assertEqual([ticket["id"] for ticket in self.ledger().ticket_open(new)], ["names-unit-2"])
        self.assertEqual(ledger.ticket_open("names-unit"), ())

    def test_unit_rename_moves_a_checkpointed_ticket_without_changing_its_merge_evidence(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.ticket_assign("names-unit-1", "main")
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, ticket="names-unit-1")
        ledger.ticket_checkpoint("names-unit-1", self.initial_tip)
        before = ledger.ticket_open("names-unit")[0]

        _ = ledger.rename_unit("names-unit", "sample-style-names-unit")

        self.assertEqual(ledger.ticket_open("sample-style-names-unit")[0],
                         {**before, "unit": "sample-style-names-unit"})
        self.assertEqual(ledger.next_ticket_id("sample-style-names-unit"), "sample-style-names-unit-1")

    def test_unit_rename_refuses_a_target_with_current_tickets_or_a_sync_wait(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.wait_for_sync("names-unit")
        old_tickets, old_waits = ledger.queued_tickets("names-unit"), ledger.sync_waits()
        for state in ("queued", "assigned", "checkpointed", "wait"):
            with self.subTest(state=state):
                target = f"{state}-style-names-unit"
                if state == "wait":
                    ledger.wait_for_sync(target)
                else:
                    ledger.ticket_add(f"{target}-1", target, "read", "names", "crates/alpha",
                                      list(self.sources[:2]), [rule])
                    with closing(sqlite3.connect(ledger.database)) as connection:
                        _ = connection.execute("UPDATE tickets SET state=? WHERE unit=?", (state, target))
                        connection.commit()
                target_waits = ledger.sync_waits()
                with self.assertRaises(style_ledger.LedgerError):
                    _ = ledger.rename_unit("names-unit", target)
                self.assertEqual(ledger.queued_tickets("names-unit"), old_tickets)
                self.assertEqual(ledger.sync_waits(), target_waits)
        self.assertTrue(all(wait in ledger.sync_waits() for wait in old_waits))

    def test_unit_rename_refuses_other_families_and_nonstyle_ids_without_moving_rows(self) -> None:
        ledger = self.ledger()
        ledger.wait_for_sync("names-unit")
        before = ledger.sync_waits()
        for old, new in (("names-unit", "sample-style-types-unit"),
                         ("names-unit", "feature-unit"), ("feature-unit", "names-unit")):
            with self.subTest(old=old, new=new), self.assertRaises(style_ledger.LedgerError):
                _ = ledger.rename_unit(old, new)
            self.assertEqual(ledger.sync_waits(), before)

    def test_unit_rename_rolls_back_ticket_updates_when_the_wait_move_fails(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.wait_for_sync("names-unit")
        tickets, waits = ledger.queued_tickets("names-unit"), ledger.sync_waits()
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("CREATE TRIGGER reject_wait_move BEFORE UPDATE OF unit ON sync_waits "
                                   + "BEGIN SELECT RAISE(ABORT, 'wait move rejected'); END")
            connection.commit()
        with self.assertRaises((sqlite3.Error, style_ledger.LedgerError)):
            _ = ledger.rename_unit("names-unit", "sample-style-names-unit")
        self.assertEqual(self.ledger().queued_tickets("names-unit"), tickets)
        self.assertEqual(self.ledger().sync_waits(), waits)
        self.assertEqual(ledger.queued_tickets("sample-style-names-unit"), ())

    def test_moved_ticket_can_be_readded_but_new_ids_must_use_the_new_prefix(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        files = list(self.sources[:2])
        ledger.ticket_add("names-unit-7", "names-unit", "read", "names", "crates/alpha", files, [rule])
        _ = ledger.rename_unit("names-unit", "sample-style-names-unit")
        before = ledger.queued_tickets("sample-style-names-unit")
        ledger.ticket_add("names-unit-7", "sample-style-names-unit", "read", "names", "crates/alpha", files, [rule])
        self.assertEqual(ledger.queued_tickets("sample-style-names-unit"), before)
        with self.assertRaises(style_ledger.LedgerError):
            ledger.ticket_add("names-unit-7", "sample-style-names-unit", "read", "names", "crates/beta", files, [rule])
        with self.assertRaises(style_ledger.LedgerError):
            ledger.ticket_add("names-unit-8", "sample-style-names-unit", "read", "names", "crates/alpha", files, [rule])
        self.assertEqual(ledger.next_ticket_id("sample-style-names-unit"), "sample-style-names-unit-1")
        ledger.ticket_add("sample-style-names-unit-1", "sample-style-names-unit", "read", "names",
                          "crates/alpha", files, [rule])
        self.assertEqual(self.ledger().next_ticket_id("sample-style-names-unit"), "sample-style-names-unit-2")
        self.assertEqual(ledger.next_ticket_id("names-unit"), "names-unit-8")

    def test_unit_rename_allows_target_history_and_moves_a_wait_without_tickets(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        target = "sample-style-names-unit"
        ledger.ticket_add(f"{target}-4", target, "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.ticket_drop(f"{target}-4")
        ledger.wait_for_sync("names-unit")
        before = ledger.sync_waits()[0]

        self.assertEqual(ledger.rename_unit("names-unit", target), style_ledger.UnitRenamed(0, 1))

        after = self.ledger().sync_waits()[0]
        self.assertEqual((after.unit, after.since, after.token), (target, before.since, before.token))
        self.assertEqual(ledger.next_ticket_id(target), f"{target}-5")
        self.assertEqual(ledger.rename_unit("names-unit", target), style_ledger.UnitRenamed(0, 0))

    def test_unit_rename_cli_reports_counts_and_succeeds_again_without_moves(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.ticket_add("names-unit-1", "names-unit", "read", "names", "crates/alpha",
                          list(self.sources[:2]), [rule])
        ledger.wait_for_sync("names-unit")
        moved = self.cli("unit", "rename", "--from", "names-unit", "--to", "sample-style-names-unit")
        self.assertRegex(moved.stdout, r"1 tickets?")
        self.assertRegex(moved.stdout, r"1 (?:sync )?waits?")
        repeated = self.cli("unit", "rename", "--from", "names-unit", "--to", "sample-style-names-unit")
        self.assertRegex(repeated.stdout, r"0 tickets?")
        self.assertRegex(repeated.stdout, r"0 (?:sync )?waits?")
        self.assertEqual([ticket["id"] for ticket in self.ledger().queued_tickets("sample-style-names-unit")],
                         ["names-unit-1"])
        self.assertEqual([wait.unit for wait in self.ledger().sync_waits()], ["sample-style-names-unit"])

    def test_next_ticket_id_never_reuses_merged_or_dropped_numbers(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        ledger.set_tip(self.initial_tip, "main")
        self.assertEqual(ledger.next_ticket_id("names-unit"), "names-unit-1")
        for number in (9, 11):
            ledger.ticket_add(f"names-unit-{number}", "names-unit", "read", "names", "crates/alpha",
                              list(self.sources[:2]), [rule])
        ledger.ticket_drop("names-unit-9")
        ledger.ticket_assign("names-unit-11", "main")
        for path in self.sources[:2]:
            self.record(rule, path, self.initial_tip, ticket="names-unit-11")
        ledger.ticket_checkpoint("names-unit-11", self.initial_tip)
        _ = ledger.ticket_merge("names-unit-11")
        self.assertEqual(ledger.next_ticket_id("names-unit"), "names-unit-12")
        ledger.ticket_add("names-unit-12", "names-unit", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        self.assertEqual(self.ledger().next_ticket_id("names-unit"), "names-unit-13")

    def test_queued_tickets_are_only_the_units_unassigned_work_in_creation_order(self) -> None:
        rule = self.rule("read")
        ledger = self.ledger()
        with mock.patch.object(style_ledger, "_now", side_effect=[
            "2026-10-16T10:00:00+00:00", "2026-10-16T10:00:01+00:00", "2026-10-16T10:00:02+00:00",
        ]):
            for number in (2, 1, 3):
                ledger.ticket_add(f"read-{number}", "read", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        ledger.ticket_assign("read-3", "main")
        ledger.ticket_add("other-1", "other", "read", "names", "crates/alpha", list(self.sources[:2]), [rule])
        self.assertEqual([ticket["id"] for ticket in ledger.queued_tickets("read")], ["read-2", "read-1"])
        ledger.ticket_drop("read-2")
        self.assertEqual([ticket["id"] for ticket in self.ledger().queued_tickets("read")], ["read-1"])

    def test_structure_ticket_keeps_sites_and_pins_their_rule_text(self) -> None:
        rule = self.rule(
            "structure", "tier: D\nfamily: modules\ncandidates:\n  kind: regex\n  pattern: value\n"
            + "  surface: partial\n  uncovered: crate structure\n",
        )
        sites: list[dict[str, object]] = [{"rule": rule, "path": self.sources[0], "line": 1, "text": "pub fn value() {}"}]
        ledger = self.ledger()
        ledger.ticket_add("modules-unit-1", "modules-unit", "structure", "modules", "crates/alpha",
                          list(self.sources[:2]), [rule], sites=sites)
        ticket, = ledger.queued_tickets("modules-unit")
        self.assertEqual((ticket["sites"], ticket["rule"]), (sites, ""))
        pinned, = ledger.pinned_rule_texts(ticket["id"])
        self.assertEqual((pinned.rule_id, pinned.text), (rule, (self.rule_dir / "structure.md").read_bytes()))

    def test_handoff_reroute_preserves_creation_and_rejects_invalid_states(self) -> None:
        ledger = self.ledger()
        ledger.handoff_add(self.initial_tip)
        pending = ledger.pending_handoffs()[0]
        refused = self.cli("handoff", "reroute", "--checkpoint", self.initial_tip, "--to", "new", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertTrue(refused.stderr.strip().endswith("; use handoff send"), refused.stderr)
        ledger.handoff_send(self.initial_tip, "old")
        sent = ledger.pending_handoffs()[0]
        with mock.patch.object(style_ledger, "_now", return_value="2099-01-01T00:00:00+00:00"):
            ledger.handoff_reroute(self.initial_tip, "new")
        rerouted = ledger.pending_handoffs()[0]
        self.assertEqual((rerouted["checkpoint"], rerouted["state"], rerouted["sent_to"], rerouted["created_at"]),
                         (self.initial_tip, "sent", "new", pending["created_at"]))
        self.assertNotEqual(rerouted["updated_at"], sent["updated_at"])
        _ = self.cli("handoff", "reroute", "--checkpoint", self.initial_tip, "--to", "new")
        self.assertEqual(ledger.pending_handoffs()[0], rerouted)
        resend = self.cli("handoff", "send", "--checkpoint", self.initial_tip, "--to", "third", success=False)
        self.assertEqual(resend.returncode, 2)
        _ = self.cli("reconcile", "--branch-point", "main", "--style-branch", "main")
        merged = self.cli("handoff", "reroute", "--checkpoint", self.initial_tip, "--to", "third", success=False)
        self.assertEqual(merged.returncode, 2)
        self.assertIn("merged", merged.stderr)
        _ = self.git("commit", "--allow-empty", "-qm", "Unknown handoff")
        unknown = self.cli("handoff", "reroute", "--checkpoint", "HEAD", "--to", "third", success=False)
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unknown", unknown.stderr.lower())

    def test_sync_waits_survive_reopen_keep_first_time_and_clear_only_the_named_unit(self) -> None:
        ledger = self.ledger()
        with mock.patch.object(style_ledger, "_now", return_value="2026-01-02T00:00:00+00:00"):
            ledger.wait_for_sync("types-unit")
            ledger.wait_for_sync("names-unit")
        first_wait = next(wait for wait in ledger.sync_waits() if wait.unit == "names-unit")
        with mock.patch.object(style_ledger, "_now", return_value="2026-01-03T00:00:00+00:00"):
            ledger.wait_for_sync("names-unit")
            ledger.wait_for_sync("modules-unit")
        with style_ledger.open_ledger(self.repo) as reopened:
            repeated_wait = next(wait for wait in reopened.sync_waits() if wait.unit == "names-unit")
            self.assertEqual(repeated_wait.token, first_wait.token)
            self.assertEqual([(wait.unit, wait.since) for wait in reopened.sync_waits()], [
                ("names-unit", "2026-01-02T00:00:00+00:00"),
                ("types-unit", "2026-01-02T00:00:00+00:00"),
                ("modules-unit", "2026-01-03T00:00:00+00:00"),
            ])
            reopened.clear_sync_wait("types-unit")
            reopened.clear_sync_wait("types-unit")
        self.assertEqual([wait.unit for wait in ledger.sync_waits()], ["names-unit", "modules-unit"])

    def test_existing_ledger_migrates_sync_waits_without_losing_tickets(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        self.add_ticket("read-1", [rule])
        database = self.ledger().database
        with closing(sqlite3.connect(database)) as connection:
            _ = connection.execute("DROP TABLE IF EXISTS sync_waits")
            _ = connection.execute("DROP TABLE IF EXISTS ticket_yields")
            _ = connection.execute("DROP TABLE IF EXISTS ticket_widened_paths")
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
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_session_dir")
            _ = connection.execute("ALTER TABLE tickets DROP COLUMN reservation_phase")
            _ = connection.execute("ALTER TABLE processed_tips DROP COLUMN reason")
            _ = connection.execute("DROP TABLE refill_results")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN built_at")
            _ = connection.execute("UPDATE schema_version SET version=6")
            connection.commit()
        with style_ledger.open_ledger(self.repo) as migrated:
            migrated.wait_for_sync("names-unit")
            self.assertEqual([wait.unit for wait in migrated.sync_waits()], ["names-unit"])
            self.assertEqual(migrated.queued_tickets("read")[0]["id"], "read-1")
            self.assertEqual(migrated.summary().synced_tip, self.initial_tip)

    def test_ticket_merge_requires_checkpoint_on_the_stored_style_branch(self) -> None:
        rule = self.rule("read")
        _ = self.git("branch", "style/main")
        _ = self.cli("set-tip", "--tip", self.initial_tip, "--style-branch", "style/main")
        self.add_ticket("read-1", [rule])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        self.write(self.sources[0], "pub fn fixed() {}\n")
        checkpoint = self.commit("Unit checkpoint")
        for path in self.sources[:2]:
            self.record(rule, path, checkpoint, ticket="read-1")
        _ = self.cli("ticket", "checkpoint", "--id", "read-1", "--commit", checkpoint)
        ledger = self.ledger()
        with self.assertRaisesRegex(style_ledger.LedgerError,
                                    f"read-1: checkpoint {checkpoint[:7]} is not on refs/heads/style/main"):
            _ = ledger.ticket_merge("read-1")
        self.assertEqual(ledger.ticket_open("read")[0]["state"], "checkpointed")
        _ = self.git("branch", "-f", "style/main", checkpoint)
        _ = ledger.ticket_merge("read-1")
        self.assertEqual(ledger.ticket_open("read"), ())
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM tickets WHERE id='read-1'").fetchone(), ("merged",))

    def test_assigned_ticket_merge_reports_the_state_before_resolving_style_branch(self) -> None:
        rule = self.rule("read")
        self.add_ticket("read-1", [rule])
        _ = self.cli("ticket", "assign", "--id", "read-1", "--branch", "main")
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            _ = connection.execute("UPDATE sync SET style_branch='refs/heads/missing'")
            connection.commit()
        refused = self.cli("ticket", "merge", "--id", "read-1", success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.strip(), "style_ledger: read-1: cannot become merged from assigned")
        self.assertEqual(self.ledger().ticket_open("read")[0]["state"], "assigned")

    def test_ticket_pin_git_failure_is_a_cli_error_and_rolls_back_the_ticket(self) -> None:
        rule = self.rule("read")
        binary = self.root / "bin"
        binary.mkdir()
        git = binary / "git"
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git is required for the ledger fixture")
        _ = git.write_text(
            "#!/bin/sh\nif [ \"$*\" = \"-C " + str(self.repo) + " hash-object --stdin\" ]; then\n"
            + "  echo 'pin hash unavailable' >&2; exit 17\nfi\nexec " + real_git + ' "$@"\n',
        )
        git.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(binary) + os.pathsep + os.environ.get("PATH", "")}):
            result = self.cli("ticket", "add", "--id", "read-1", "--unit", "read", "--kind", "read",
                              "--family", "names", "--crate", "crates/alpha", "--files", json.dumps([self.sources[0]]),
                              "--rules", json.dumps([rule]), success=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "style_ledger: pin hash unavailable")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.ledger().queued_tickets("read"), ())
        with closing(sqlite3.connect(self.ledger().database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rule_texts").fetchone(), (0,))


if __name__ == "__main__":
    _ = unittest.main()
