"""Split accounting, cross-production selection and refill recording."""

from __future__ import annotations

from scripts.style.ledger_test_support import ticket_owner_id

import io
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import chdir, closing, nullcontext, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from typing import cast, final, override
from unittest import mock


from scripts.style import approved_renames
from scripts.style import declarations
from scripts.style import style_plan
from scripts.production.staff_test_support import fixture_member, isolated_staff_file, production_membership, recorded_showrunner, recorded_unit
from scripts.production import live_units
from scripts.style import unit_plans
from scripts.style import style_proposals

from scripts.lib import berth_board
from scripts.style import rules
from scripts.style import style_ledger
from scripts.production import style_units

from scripts.style.style_plan_test_support import StylePlanFixture, unit_staff_id


@final
class StylePlanTests(StylePlanFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = recorded_showrunner("sample", self.repo / "docs/product-production.md")
        _ = recorded_unit("sample-style", "names", self.unit, self.doc,
                          plan="docs/style-production/units/names-unit.md")

    def test_split_partitions_the_table_left_total_including_ticketed_and_unselected_findings(self) -> None:
        rule = self.rule("split-population")
        names = ("free", "ticketed", "judged", "unsynced", "structure", "exempt", "busy", "gone",
                 "unknown", "closed", "declaration")
        paths = {name: f"crates/alpha/src/{name}.rs" for name in names}
        paths["outside"] = "crates/beta/src/outside.rs"
        self.commit_files({path: f"pub fn {name}() {{}}\n" for name, path in paths.items()})
        issue, findings = self.judged_findings(rule, tuple((path, 1, f"pub fn {name}() {{}}")
                                                        for name, path in paths.items()))
        ledger = self.ledger()
        by_path = {finding.path: finding for finding in findings}
        for name in ("unknown", "closed"):
            decision = issue.with_name(name + ".md")
            text = issue.read_text()
            text = text.replace(rule, "global:rust/removed-rule.md") if name == "unknown" else text.replace("status: open", "status: closed")
            _ = decision.write_text(text)
            with closing(sqlite3.connect(ledger.database)) as connection, connection:
                _ = connection.execute("UPDATE held_findings SET proposal=? WHERE finding_id=?",
                                       (str(decision), by_path[paths[name]].finding_id))
        ledger.ticket_add("2", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [paths["ticketed"]],
                          [rule], proposal=str(issue), finding_ids=(by_path[paths["ticketed"]].finding_id,))
        ledger.ticket_assign("2", "demo-style-names")
        for index, excluded in enumerate(("declined", "unanswered", "types", "waived", "unfiled", "built"), 2):
            decision = issue.with_name(excluded + ".md")
            text = issue.read_text()
            if excluded == "declined":
                text = text.replace("plan: approved", "plan: approved\nstyle_answer: declined")
            elif excluded == "unanswered":
                text = re.sub(r"unit: [^\n]*\n", "", text)
            elif excluded == "types":
                _ = fixture_member("unit", name="types", parent=recorded_showrunner("sample-style", self.doc).id)
                text = text.replace(f"unit: {unit_staff_id('names')}", f"unit: {unit_staff_id('types')}")
            _ = decision.write_text(text)
            finding = ledger.hold(rule, paths["free"], index, excluded, "type", "before", "after",
                                  findings[0].rule_blob, "outside")
            if excluded != "unfiled":
                ledger.file_held(finding.finding_id, decision)
            if excluded in ("waived", "built"):
                with closing(sqlite3.connect(ledger.database)) as connection, connection:
                    _ = connection.execute("UPDATE held_findings SET state=? WHERE finding_id=?",
                                           (excluded, finding.finding_id))
        self.commit_files({paths["judged"]: "pub fn judged() { changed(); }\n"})
        _ = (self.repo / paths["gone"]).unlink()
        self.commit_to_every_checkout()
        outcomes: tuple[style_ledger.DeclarationOutcome, ...] = tuple(
            style_ledger.DeclarationGoneFromFile(finding, declarations.Declaration("fn", "declaration", declarations.ModuleItem()))
            if finding.path == paths["declaration"] else style_ledger.DeclarationNotRead(finding) for finding in findings)
        waits = {paths[name]: style_proposals.RankWait("names", paths[name], "types")
                 for name in ("structure", "exempt", "judged", "unsynced", "ticketed")}

        def exempt(finding: style_ledger.HeldFinding, _family: rules.Family) -> bool:
            return finding.path == paths["exempt"]

        self.finish_judging()
        definitions = {definition.rule_id: definition for definition in rules.load_rules(self.repo)}
        measured_at = "2026-10-28T12:00:00+00:00"
        with (mock.patch.object(style_ledger.Ledger, "declaration_reading", return_value=style_ledger.DeclarationReading(self.tip, outcomes)),
              mock.patch.object(style_proposals.RankedWork, "waits", return_value=waits),
              mock.patch.object(style_proposals, "ranked_below_unit", return_value=True),
              mock.patch.object(style_proposals.RankExemptions, "allows", side_effect=exempt),
              mock.patch.object(style_plan, "unsynced_changes", return_value={
                  paths["unsynced"]: style_plan.UnsyncedChange(paths["unsynced"], self.tip)})):
            split = style_plan.classify_split(ledger, "names", self.doc, self.base(),
                                              unit_plans.PilotCrates(frozenset({"crates/alpha"})),
                                              style_plan.BusyFiles(self.board(), frozenset({paths["busy"], paths["ticketed"]})),
                                              definitions, style_ledger.GitReadCache(self.repo), measured_at)
        self.assertEqual(split, style_ledger.RecordedSplit(3, 2, 1, 6, measured_at))
        left = split.ready + split.changed + split.structure + split.other
        output = style_units.table([live_units.LiveUnit("names", None)], {}, doc=self.doc, now=datetime(2026, 10, 28, 12, tzinfo=UTC),
                                   zone=UTC, ledger_paths={"names": ledger.database})
        self.assertIn(f"| 1 | 0 | {left} approved of 14 filed |", output)

    def test_a_merged_ticket_keeps_its_filed_type_finding_in_other_until_a_person_marks_it_built(self) -> None:
        rule = self.rule("split-merged-type")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:1], "type")
        ledger = self.ledger()
        finding, = ledger.proposal_findings(issue)
        ledger.ticket_add("1", ticket_owner_id("names"), "proposal", "names", "crates/alpha", [finding.path],
                          [rule], proposal=str(issue), finding_ids=(finding.finding_id,))
        ledger.ticket_assign("1", "demo-style-names")
        ledger.ticket_checkpoint("1", self.tip)
        self.assertEqual(ledger.ticket_merge("1"),
                         (style_ledger.FindingNotBuilt(finding.finding_id, approved_renames.NoAppliedCheck("type")),))
        held, = ledger.proposal_findings(issue)
        self.assertIsInstance(held.state, style_ledger.Filed)
        definitions = {definition.rule_id: definition for definition in rules.load_rules(self.repo)}
        measured_at = "2026-10-28T12:00:00+00:00"
        with (mock.patch.object(ledger, "declaration_reading", return_value=style_ledger.DeclarationReading(
                  self.tip, (style_ledger.DeclarationNotRead(held),))),
              mock.patch.object(style_proposals.RankedWork, "waits", return_value={}),
              mock.patch.object(style_proposals, "ranked_below_unit", return_value=False)):
            result = style_plan.classify_split(ledger, "names", self.doc, self.base(), unit_plans.AllCrates(),
                                               style_plan.BusyFiles(self.board(), frozenset()), definitions,
                                               style_ledger.GitReadCache(self.repo), measured_at)
        self.assertEqual(result, style_ledger.RecordedSplit(0, 0, 0, 1, measured_at))
        self.assertEqual(ledger.proposal_findings(issue), (held,))

    def test_relative_proposals_use_the_table_population_and_refills_exact_stored_key(self) -> None:
        rule = self.rule("split-relative-proposal")
        self.sync()
        original = self.approved_decision(rule, self.sources[:2], "type")
        ledger = self.ledger()
        folder = self.repo / "decisions"
        folder.mkdir()
        issue = folder / "a.md"
        _ = issue.write_text(original.read_text())
        _ = self.doc.write_text(self.doc.read_text().replace(str(original.parent), str(folder)))
        findings = ledger.proposal_findings(original)
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            for finding, path in zip(findings, (str(issue), "decisions/a.md"), strict=True):
                _ = connection.execute("UPDATE held_findings SET proposal=? WHERE finding_id=?", (path, finding.finding_id))
        definitions = {definition.rule_id: definition for definition in rules.load_rules(self.repo)}
        measured_at = "2026-10-28T12:00:00+00:00"
        with (chdir(self.repo),
              mock.patch.object(ledger, "declaration_reading", return_value=style_ledger.DeclarationReading(
                  self.tip, tuple(style_ledger.DeclarationNotRead(finding) for finding in findings))),
              mock.patch.object(style_proposals.RankedWork, "waits", return_value={}),
              mock.patch.object(style_proposals, "ranked_below_unit", return_value=False)):
            result = style_plan.classify_split(ledger, "names", self.doc, self.base(), unit_plans.AllCrates(),
                                               style_plan.BusyFiles(self.board(), frozenset()), definitions,
                                               style_ledger.GitReadCache(self.repo), measured_at)
            table = style_units.table([live_units.LiveUnit("names", None)], {}, doc=self.doc,
                                      ledger_paths={"names": ledger.database})
        self.assertEqual(result, style_ledger.RecordedSplit(1, 0, 0, 1, measured_at))
        self.assertIn("| 0 | 0 | 2 |", table)
        self.assertEqual(result.ready + result.changed + result.structure + result.other, 2)

    def test_refill_and_split_readers_agree_on_rank_waits_and_changed_judgments(self) -> None:
        name_rule = self.rule("shared-name-inputs")
        type_rule = self.rule("shared-type-inputs", "tier: C\nfamily: types\ninput_scope: file\n")
        self.sync()
        issue, findings = self.judged_findings(name_rule, tuple((path, 1, "pub fn value() {}")
                                                              for path in self.sources[:2]))
        ledger = self.ledger()
        # The helper files each finding after hold(); site evidence consumes the current ledger row.
        findings = ledger.proposal_findings(issue)
        definition = next(item for item in rules.load_rules(self.repo) if item.rule_id == type_rule)
        ledger.ticket_add("2001", ticket_owner_id("types"), "read", "types", "crates/alpha", [self.sources[1]], [type_rule])
        ledger.ticket_assign("2001", "style/main")
        _ = ledger.ticket_base("2001")
        for index, path in enumerate(self.sources[:2]):
            _ = ledger.hold(type_rule, path, 1, "type work", "type", "before", "after", definition.blob,
                            "outside" if index == 0 else "2001")
        ledger.ticket_drop("2001")
        self.commit_files({self.sources[1]: "pub fn value() { changed(); }\n"})
        self.finish_judging()
        scopes: dict[rules.Family, unit_plans.PilotScope] = {"names": unit_plans.AllCrates(), "types": unit_plans.AllCrates()}
        cache = style_ledger.GitReadCache(self.repo)
        expected = {self.sources[0]: style_proposals.RankWait("names", self.sources[0], "types")}
        self.assertEqual(style_proposals.ranked_work(ledger, (), scopes).waits("names"), expected)
        self.assertEqual(style_proposals.ranked_work(ledger, (), scopes, reader=cache).waits("names"), expected)
        with style_ledger.Ledger(self.repo, ledger.database, git_cache=cache) as cached_ledger:
            for finding in findings:
                with self.subTest(path=finding.path):
                    unchanged = finding.path == self.sources[0]
                    self.assertEqual(style_proposals.judged_at_current_file(ledger, finding, self.tip), unchanged)
                    self.assertEqual(style_proposals.judged_at_current_file(cached_ledger, finding, self.tip), unchanged)
        reading = style_ledger.DeclarationReading(self.tip, tuple(style_ledger.DeclarationNotRead(finding) for finding in findings))
        definitions = {item.rule_id: item for item in rules.load_rules(self.repo)}
        measured_at = "2026-10-28T12:00:00+00:00"
        output = io.StringIO()
        with (mock.patch.object(style_plan, "family_pilot_scopes", return_value=scopes),
              mock.patch.object(style_proposals.RankExemptions, "allows", return_value=False),
              mock.patch.object(style_proposals, "run_follow", return_value=style_proposals.FollowRun(reading, (), ())),
              mock.patch.object(ledger, "declaration_reading", return_value=reading), redirect_stdout(output)):
            split = style_plan.classify_split(ledger, "names", self.doc, self.base(), unit_plans.AllCrates(),
                                              style_plan.BusyFiles(self.board(), frozenset()), definitions, cache, measured_at)
            _ = style_plan._select_phase(ledger, ticket_owner_id("names"), "demo-style-names", self.plan, self.repo,  # pyright: ignore[reportPrivateUsage]
                                         self.doc, self.plan.read_text(), unit_plans.AllCrates(),
                                         style_plan.BusyFiles(self.board(), frozenset()), frozenset(), definitions, style_plan.MainChanges(self.tip), family="names")
        self.assertEqual(split, style_ledger.RecordedSplit(0, 1, 1, 0, measured_at))
        self.assertIn(f"waiting names: {self.sources[0]} has open types work\n", output.getvalue())
        changed = next(finding for finding in findings if finding.path == self.sources[1])
        self.assertIn(f"waiting {issue}: {changed.path} changed since it was judged (1 finding)\n", output.getvalue())


    def test_split_changes_only_refill_results_and_keeps_the_plan_and_decisions(self) -> None:
        rule = self.rule("read-only-split")
        self.sync()
        issue = self.approved_decision(rule, self.sources[:1], "type")
        ledger = self.ledger()
        findings = ledger.proposal_findings(issue)
        before_plan, before_issue = self.plan.read_bytes(), issue.read_bytes()
        with closing(sqlite3.connect(ledger.database)) as connection:
            before = tuple(connection.iterdump())
        output, errors = io.StringIO(), io.StringIO()
        started = datetime.now(UTC)
        with (mock.patch.object(style_plan, "style_document", return_value=self.doc),
              mock.patch.object(style_plan, "busy_files", return_value=style_plan.BusyFiles(self.board(), frozenset())),
              mock.patch.object(style_ledger.Ledger, "declaration_reading", return_value=style_ledger.DeclarationReading(
                  self.tip, tuple(style_ledger.DeclarationNotRead(finding) for finding in findings))),
              mock.patch.object(style_ledger, "refill_lock", side_effect=AssertionError("split takes no refill lock")),
              mock.patch.object(style_ledger.Ledger, "ticket_add", side_effect=AssertionError("split makes no ticket")),
              mock.patch.object(style_ledger.Ledger, "decline_findings", side_effect=AssertionError("split waives nothing")),
              mock.patch.object(style_proposals, "run_follow", side_effect=AssertionError("split applies no declaration follow")),
              mock.patch.object(style_proposals, "proposal_command", side_effect=AssertionError("split changes no decision")),
              mock.patch.object(style_proposals, "renumber", side_effect=AssertionError("split does not renumber")),
              mock.patch.object(style_plan, "write_phase", side_effect=AssertionError("split writes no phase")),
              redirect_stdout(output), redirect_stderr(errors)):
            code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit)])
        self.assertEqual((code, errors.getvalue()), (0, ""))
        self.assertRegex(output.getvalue(), r"^names: 1/0/0/0 in \d+\.\d{2} min\n$")
        with closing(sqlite3.connect(ledger.database)) as connection:
            after = tuple(line for line in connection.iterdump() if not line.startswith('INSERT INTO "refill_results"'))
            row = cast(tuple[str, str, str, int, int, int, int], connection.execute(
                "SELECT at,outcome,split_at,ready,changed,structure,other FROM refill_results WHERE unit='names'").fetchone())
        self.assertEqual(after, before)
        self.assertEqual((row[0], row[1], *row[3:]), ("", "", 1, 0, 0, 0))
        measured_at = datetime.fromisoformat(row[2])
        self.assertEqual(measured_at.utcoffset(), UTC.utcoffset(measured_at))
        self.assertLessEqual(started, measured_at)
        self.assertLessEqual(measured_at, datetime.now(UTC))
        self.assertEqual((self.plan.read_bytes(), issue.read_bytes()), (before_plan, before_issue))
        rendered = style_units.table([live_units.LiveUnit("names", None)], {}, doc=self.doc, now=measured_at, zone=UTC,
                                     ledger_paths={"names": ledger.database})
        self.assertIn("| names | active | idle | idle | — | 0 | 0 | 1: ready 1 · changed 0 · structure 0 · other 0 (0 min) |", rendered)

    def test_a_scan_error_or_time_limit_keeps_the_last_split_and_reports_the_cause(self) -> None:
        _ = self.rule("failed-split")
        self.sync()
        ledger = self.ledger()
        style_ledger.record_split(self.repo, "names", style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00"))
        before = ledger.database.read_bytes()

        def expire(*_args: object, **_kwargs: object) -> None:
            signal.raise_signal(signal.SIGALRM)

        for failure, cause in ((OSError("cannot read tree"), "cannot read tree"), (expire, "time limit")):
            with self.subTest(cause=cause):
                output, errors = io.StringIO(), io.StringIO()
                with (mock.patch.object(style_plan, "style_document", return_value=self.doc),
                      mock.patch.object(style_plan, "busy_files", return_value=style_plan.BusyFiles(self.board(), frozenset())),
                      mock.patch.object(style_plan, "classify_split", side_effect=failure),
                      mock.patch.object(style_ledger, "record_split", wraps=style_ledger.record_split) as record,
                      redirect_stdout(output), redirect_stderr(errors)):
                    code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit), "--timeout-minutes", "1"])
                self.assertEqual((code, output.getvalue()), (1, ""))
                self.assertTrue(errors.getvalue().startswith("split names: "))
                self.assertIn(cause, errors.getvalue())
                record.assert_not_called()
                self.assertEqual(ledger.database.read_bytes(), before)

    def test_split_write_stops_at_its_deadline_on_a_locked_ledger(self) -> None:
        _ = self.rule("write-time-limit")
        self.sync()
        ledger = self.ledger()
        style_ledger.record_split(self.repo, "names", style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00"))
        with closing(sqlite3.connect(ledger.database)) as connection:
            before = tuple(connection.iterdump())
        output, errors = io.StringIO(), io.StringIO()

        limit_seconds = 0.25
        deadline = style_plan.SplitDeadline(limit_seconds, limit_seconds / 60)
        busy_budgets: list[tuple[int]] = []
        record_split = style_ledger.record_split

        connect = sqlite3.connect

        def observe_connect(database: str, *, uri: bool, timeout: float) -> sqlite3.Connection:
            connection = connect(database, uri=uri, timeout=timeout)
            busy_budgets.append(cast(tuple[int], connection.execute("PRAGMA busy_timeout").fetchone()))
            return connection

        def bounded_write(repo: Path, unit: str, split: style_ledger.RecordedSplit, *, busy_timeout_seconds: float) -> None:
            with mock.patch.object(sqlite3, "connect", side_effect=observe_connect):
                record_split(repo, unit, split, busy_timeout_seconds=busy_timeout_seconds)

        # Scan setup cannot spend the write budget. Without SIGALRM, SQLite itself
        # must bound the real lock wait using the remaining deadline passed by split.
        with (closing(sqlite3.connect(ledger.database)) as writer,
              mock.patch.object(style_plan, "split_time_limit", return_value=nullcontext(deadline)),
              mock.patch.object(style_plan, "time", wraps=time, monotonic=mock.Mock(return_value=0.0)),
              mock.patch.object(style_plan, "style_document", return_value=self.doc),
              mock.patch.object(style_plan, "busy_files", return_value=style_plan.BusyFiles(self.board(), frozenset())),
              mock.patch.object(style_plan, "classify_split", return_value=style_ledger.RecordedSplit(
                  1, 2, 3, 4, "2026-10-28T12:00:00+00:00")),
              mock.patch.object(style_ledger, "record_split", side_effect=bounded_write) as record,
              redirect_stdout(output), redirect_stderr(errors)):
            _ = writer.execute("BEGIN IMMEDIATE")
            code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit),
                                    "--timeout-minutes", str(limit_seconds / 60)])
            writer.rollback()
        self.assertEqual((code, output.getvalue()), (1, ""))
        self.assertRegex(errors.getvalue(), r"^split names: (time limit.*|database is locked)\n$")
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["busy_timeout_seconds"], limit_seconds)
        self.assertEqual(busy_budgets, [(250,)])
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(tuple(connection.iterdump()), before)

    def test_split_write_is_refused_after_its_deadline(self) -> None:
        _ = self.rule("expired-write")
        self.sync()
        ledger = self.ledger()
        style_ledger.record_split(self.repo, "names", style_ledger.RecordedSplit(9, 8, 7, 6, "2026-10-27T12:00:00+00:00"))
        before = ledger.database.read_bytes()
        deadline = style_plan.SplitDeadline(time.monotonic() - 1, 1)
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(style_plan, "split_time_limit", return_value=nullcontext(deadline)),
              mock.patch.object(style_plan, "style_document", return_value=self.doc),
              mock.patch.object(style_plan, "busy_files", return_value=style_plan.BusyFiles(self.board(), frozenset())),
              mock.patch.object(style_plan, "classify_split", return_value=style_ledger.RecordedSplit(
                  1, 2, 3, 4, "2026-10-28T12:00:00+00:00")),
              mock.patch.object(style_ledger, "record_split", wraps=style_ledger.record_split) as record,
              redirect_stdout(output), redirect_stderr(errors)):
            code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit)])
        self.assertEqual((code, output.getvalue(), errors.getvalue()),
                         (1, "", "split names: time limit of 1 min reached\n"))
        record.assert_not_called()
        self.assertEqual(ledger.database.read_bytes(), before)

    def test_a_ledger_without_refill_results_is_refused_before_any_scan_or_migration(self) -> None:
        _ = self.rule("old-schema-split")
        self.sync()
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("DROP TABLE refill_results")
        before = ledger.database.read_bytes()
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(style_ledger, "preview_ledger", side_effect=AssertionError("no backup or scan of the old schema")),
              mock.patch.object(style_plan, "classify_split", side_effect=AssertionError("no long scan")),
              mock.patch.object(style_ledger, "record_split", side_effect=AssertionError("no split write")),
              redirect_stdout(output), redirect_stderr(errors)):
            code = style_plan.main(["split", "--unit", "names", "--repo", str(self.unit)])
        self.assertEqual((code, output.getvalue(), errors.getvalue()),
                         (1, "", "split names: ledger has no refill_results (schema below 25)\n"))
        self.assertEqual(ledger.database.read_bytes(), before)


class UnsyncedChangeTests(unittest.TestCase):
    def test_a_base_commit_not_in_the_synced_tip_names_each_file_it_changed_and_its_newest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)

            def commit(path: str, text: str) -> str:
                _ = (repo / path).write_text(text)
                for arguments in (("add", path), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", path)):
                    _ = subprocess.run(["git", "-C", folder, *arguments], check=True)
                return subprocess.run(["git", "-C", folder, "rev-parse", "HEAD"], check=True, capture_output=True,
                                      text=True).stdout.strip()

            _ = subprocess.run(["git", "init", "-q", folder], check=True)
            tip = commit("a.rs", "fn a() {}\n")
            first = commit("b.rs", "fn b() {}\n")
            newest = commit("b.rs", "fn b() { c(); }\n")
            changes = style_plan.unsynced_changes(repo, style_plan.UnitBranchTip(newest), tip)
            self.assertEqual(dict(changes), {"b.rs": style_plan.UnsyncedChange("b.rs", newest)})
            self.assertNotEqual(first, newest)
            self.assertEqual(changes["b.rs"].line(tip), f"b.rs changed in {newest[:7]}, not in the synced tip {tip[:7]}")
            # Only the first path of a commit loses the newline that `-z` puts before it; a tab path sorts first.
            for path in ("\tfirst.rs", "\nline.rs"):
                _ = (repo / path).write_text("text\n")
            for arguments in (("add", "--all"), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "two")):
                _ = subprocess.run(["git", "-C", folder, *arguments], check=True)
            both = subprocess.run(["git", "-C", folder, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(set(style_plan.unsynced_changes(repo, style_plan.UnitBranchTip(both), newest)), {"\tfirst.rs", "\nline.rs"})
            # A base inside the synced tip, and no unit branch, change no file.
            self.assertEqual(dict(style_plan.unsynced_changes(repo, style_plan.UnitBranchTip(tip), newest)), {})
            self.assertEqual(dict(style_plan.unsynced_changes(repo, style_plan.NoUnitBranch(), tip)), {})


class RefillRecordingTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(isolated_staff_file(root / "staff.json", {}))
        production_membership("production")

    def test_main_records_every_outcome_after_next_phase_returns(self) -> None:
        cases: tuple[tuple[object, str, int, str], ...] = (
            (style_plan.PhaseWritten("1"), "phase written", 0, "phase written: 1\n"),
            (style_plan.Resumed("1"), "resumed", 0, "resume 1\n"),
            (style_plan.RepairFirst(), "repair first", 0, "repair first\n"),
            (style_plan.WaitingForMerge("1", "abcdef123"), "waiting for merge", 5,
             "waiting for merge: 1 checkpointed at abcdef1\n"),
            (style_plan.WaitingForSync(0), "waiting for sync", 3, "waiting for sync\n"),
            (style_plan.WaitingForStructure(("alpha", "beta"), 0), "waiting for structure: alpha, beta", 3,
             "waiting for structure: alpha, beta\n"),
            (style_plan.WaitingForSync(0, "read alpha: main touched alpha/lib.rs"),
             "waiting for sync: read alpha: main touched alpha/lib.rs", 3,
             "waiting for sync: read alpha: main touched alpha/lib.rs\n"),
            (style_plan.MergeStyleBranchFirst(1, "abcdef123", "refs/heads/style/main"),
             "merge the style branch first", 6, "merge the style branch first: 1 cell judged at abcdef1 wait for style/main\n"),
            (berth_board.BoardUnavailable("offline"), "board unavailable", 4, "board unavailable: offline\n"),
        )
        for result, words, code, printed in cases:
            with self.subTest(outcome=words, result=result):
                output = io.StringIO()
                calls: list[str] = []

                def next_result(_unit: str, _plan: Path, _repo: Path) -> object:
                    calls.append("returned")
                    return result

                def record(_repo: Path, _unit: str, _outcome: str) -> None:
                    calls.append("recorded")

                with (mock.patch.object(style_plan, "next_phase", side_effect=next_result),
                      mock.patch.object(style_ledger, "record_refill", side_effect=record) as recorded,
                      redirect_stdout(output)):
                    self.assertEqual(style_plan.main(["next-phase", "--unit", "names", "--plan", "/tmp/claude/plan.md",
                                                      "--repo", "/tmp/claude/repo"]), code)
                recorded.assert_called_once_with(Path("/tmp/claude/repo"), "names", words)
                self.assertEqual(calls, ["returned", "recorded"])
                self.assertEqual(output.getvalue(), printed)

    def test_an_exception_from_next_phase_records_nothing(self) -> None:
        for error in (OSError("cannot read"), ValueError("invalid plan"), sqlite3.OperationalError("database error")):
            with self.subTest(error=error):
                output = io.StringIO()
                with (mock.patch.object(style_plan, "next_phase", side_effect=error),
                      mock.patch.object(style_ledger, "record_refill") as recorded, redirect_stderr(output)):
                    self.assertEqual(style_plan.main(["next-phase", "--unit", "names", "--plan", "/tmp/claude/plan.md",
                                                      "--repo", "/tmp/claude/repo"]), 2)
                recorded.assert_not_called()
                self.assertIn(str(error), output.getvalue())




from scripts.production.staff_test_support import StaffTestCase
from scripts.style import unit_files


class BranchFileLocationTests(StaffTestCase):
    def test_file_ownership_uses_the_staff_worktree(self) -> None:
        fixture = self.separated_unit_folders()
        rows = unit_files.unit_worktrees(fixture.doc)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].worktree, fixture.worktree)
        self.assertEqual(rows[0].unit, "member")


if __name__ == "__main__":
    _ = unittest.main()
