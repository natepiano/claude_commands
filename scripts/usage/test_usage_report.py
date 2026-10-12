"""Compare store-only usage reports with independently computed Markdown fixtures."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast, final, override
from zoneinfo import ZoneInfo

from scripts.usage.test_collect import FIXTURES as COLLECT_FIXTURES, MacStreamReport, REPOSITORY, SNAPSHOT_COMMIT_SCRIPT, SshBehavior
from scripts.usage.test_outcomes import OutcomeFixture


FIXTURES = Path(__file__).with_name("fixtures") / "report"
REPORT = Path(__file__).with_name("usage_report.py")
METRICS = ("weighted", "weighted_per_checkpoint", "before_first_edit_share", "cache_read_share",
           "output_share", "compactions_per_thread", "weighted_per_finding_closed",
           "weighted_per_review_round", "account_switches")


class ReportFunctionCalls(TypedDict):
    metric: str
    by: str
    calls: dict[str, int]


class EquivalentSideReport(TypedDict):
    metric: str
    by: str
    additive: str
    general: str


class ReportFixture(OutcomeFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        for directory in (self.claude_root, self.codex_root, self.history):
            shutil.rmtree(directory)
        _ = shutil.copytree(FIXTURES / "home", self.root, dirs_exist_ok=True)
        _ = shutil.copytree(FIXTURES / "production-docs", self.production_docs, dirs_exist_ok=True)
        _ = self.commit("checkpoint(report-plan): phase 7 — First", "2026-10-10T21:37:00Z")
        _ = self.commit("checkpoint(report-plan): phase 7 — Second", "2026-10-10T21:38:00Z")
        _ = self.commit("checkpoint(other-plan): phase 7 — Unmapped", "2026-10-11T07:01:00Z")
        _ = self.collect()

    def report_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(HOME=str(self.root), USAGE_STATE_DIR=str(self.state), PYTHONDONTWRITEBYTECODE="1")
        return environment

    def report(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(REPORT), "--db", str(self.db_path),
                               "--production-docs", str(self.production_docs), *arguments],
                              env=self.report_environment(), cwd=REPOSITORY, capture_output=True,
                              text=True, check=False, timeout=10)

    def report_script(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-c", script, "--db", str(self.db_path),
                               "--production-docs", str(self.production_docs), *arguments],
                              env=self.report_environment(), cwd=REPOSITORY, capture_output=True,
                              text=True, check=False, timeout=30)

    def mac_complete_times(self) -> dict[str, str]:
        rows = self.rows("SELECT family, last_complete_at FROM turn_sources WHERE machine = 'mac' ORDER BY family")
        self.assertEqual([cast(str, row["family"]) for row in rows], ["claude", "codex", "history"])
        for row in rows:
            self.assertIsInstance(cast(object, row["last_complete_at"]), str)
        return {cast(str, row["family"]): cast(str, row["last_complete_at"]) for row in rows}

    def expected_report(self, name: str, zone: str = "America/Los_Angeles") -> str:
        expected = (FIXTURES / "expected" / f"{name}.md").read_text(encoding="utf-8")
        if "Sources\n" in expected:
            self.assertEqual(self.values(self.rows(
                "SELECT state, last_complete_at FROM turn_sources " +
                "WHERE machine = 'natedev' AND family = 'baseline_readings'"), "state", "last_complete_at"),
                [("missing", None)])
        for row in self.rows("SELECT machine, family, last_complete_at FROM turn_sources"):
            stamp = cast(object, row["last_complete_at"])
            if isinstance(stamp, str):
                value = datetime.fromisoformat(stamp).astimezone(ZoneInfo(zone)).isoformat()
                expected = expected.replace(f"{{{{{row['machine']}.{row['family']}.complete}}}}", value)
        for row in self.rows("SELECT machine, read_at FROM role_log_sources"):
            stamp = datetime.fromisoformat(cast(str, row["read_at"])).astimezone(ZoneInfo(zone)).isoformat()
            expected = expected.replace(f"{{{{{row['machine']}.roles.read}}}}", stamp)
        self.assertNotIn("{{", expected, "Every timestamp placeholder comes from this fixture store")
        return expected

    def assert_report(self, name: str, *arguments: str, zone: str = "America/Los_Angeles") -> None:
        result = self.report(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, self.expected_report(name, zone))

    def mac_pass(self, behavior: SshBehavior) -> MacStreamReport:
        mac_home = self.root / "fixture-mac-home"
        if not mac_home.exists():
            _ = shutil.copytree(FIXTURES / "home", mac_home)
            # A first-file cut preserves only this parent; other fixture agents stay local.
            for path in (mac_home / ".claude/projects/project").glob("*.jsonl"):
                if path.name != "report-parent.jsonl":
                    path.unlink()
            shutil.rmtree(mac_home / ".claude/projects/project/report-parent")
            shutil.rmtree(mac_home / ".codex/sessions")
            (mac_home / ".codex/sessions").mkdir()
            _ = (mac_home / ".local/state/plan-delegate/runs/report-run.jsonl").write_bytes(b"")
            (mac_home / ".local/state/usage-model/agents.jsonl").unlink()
            (self.root / "outside-checkout").mkdir()
            (self.root / "bin").mkdir()
            ssh = self.root / "bin/ssh"
            _ = shutil.copyfile(COLLECT_FIXTURES / "mac/fake_ssh.py", ssh)
            ssh.chmod(0o700)
        report_path = self.root / "ssh-report.json"
        self.assertFalse(report_path.exists())
        configuration = {"home": str(mac_home), "execution_directory": str(self.root / "outside-checkout"),
                         "report": str(report_path), "behavior": behavior, "database": str(self.db_path),
                         "previous_role_read_at": ""}
        _ = (self.root / "ssh-configuration.json").write_text(json.dumps(configuration), encoding="utf-8")
        _ = self.collect(mac_pass="fixture")
        stream = cast(MacStreamReport, json.loads(report_path.read_text(encoding="utf-8")))
        report_path.unlink()
        return stream


@final
class UsageReportTests(ReportFixture):
    def test_report_reader_keeps_working_in_the_previous_journal_mode(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        self.assert_report("weighted", "weighted")

    def test_report_figures_and_sources_share_one_snapshot_during_a_writer_commit(self) -> None:
        expected = self.expected_report("weighted")
        script = SNAPSHOT_COMMIT_SCRIPT + """
from scripts.usage import usage_report
with patch.object(sqlite3, 'connect', snapshot_connect):
    result = usage_report.main(sys.argv[1:])
assert changed
raise SystemExit(result)
"""
        result = self.report_script(script, "weighted")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, expected)
        self.assertEqual(self.rows("SELECT count(*) AS turns FROM turns WHERE family = 'codex'")[0]["turns"], 0)
        self.assertEqual(self.rows("SELECT state FROM turn_sources WHERE machine = 'natedev' AND family = 'codex'")[0]["state"],
                         "unreachable")

    def test_list_names_each_metric_with_its_description(self) -> None:
        self.assert_report("list", "list")

    def test_every_initial_metric_uses_one_aggregate_per_side(self) -> None:
        for metric in METRICS:
            with self.subTest(metric=metric):
                arguments = (metric, "--by", "day,machine") if metric == "account_switches" else (metric,)
                self.assert_report(metric, *arguments)

    def test_model_switch_uses_each_weight_in_a_bounded_utc_window(self) -> None:
        self.assert_report("switch-window", "weighted", "--by", "day,model", "--zone", "UTC",
                           "--since", "2026-10-10T21:34:59Z", "--until", "2026-10-10T21:35:01Z", zone="UTC")

    def test_datetime_without_offset_is_read_in_the_requested_zone(self) -> None:
        self.assert_report("switch-local", "weighted", "--by", "day,model",
                           "--since", "2026-10-10T14:34:59", "--until", "2026-10-10T14:35:01")

    def test_day_uses_user_zone_and_datetime_bounds_are_half_open(self) -> None:
        self.assert_report("day-model", "weighted", "--by", "day,model")
        self.assert_report("midnight-naive", "weighted", "--by", "day,model",
                           "--since", "2026-10-11", "--until", "2026-10-12")
        self.assert_report("midnight-offset", "weighted", "--by", "day,model", "--zone", "UTC",
                           "--since", "2026-10-11T06:59:59Z", "--until", "2026-10-11T07:00:00Z", zone="UTC")

    def collect_time_fixture(self, name: str) -> None:
        for directory in (self.claude_root, self.codex_root, self.history):
            shutil.rmtree(directory)
            directory.mkdir(parents=True)
        project = self.claude_root / "project"
        project.mkdir()
        _ = shutil.copyfile(FIXTURES / "time-zones" / f"{name}.jsonl", project / "time-zone-session.jsonl")
        _ = self.agents_path.write_bytes(b"")
        self.db_path.unlink()
        _ = self.collect()

    def test_fractional_offset_midnight_keeps_subsecond_turns_on_their_local_days(self) -> None:
        self.collect_time_fixture("fractional-midnight")
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 2)
        self.assert_report("fractional-midnight", "weighted", "--by", "day,model", "--zone", "Asia/Kathmandu",
                           "--since", "2026-10-10", "--until", "2026-10-12", zone="Asia/Kathmandu")

    def test_offset_transition_keeps_subsecond_turns_and_uses_the_new_offset_at_midnight(self) -> None:
        self.collect_time_fixture("offset-transition")
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 6)
        self.assert_report("offset-transition", "weighted", "--by", "day,model", "--zone", "America/Los_Angeles",
                           "--since", "2026-10-31", "--until", "2026-11-03")

    def test_subagents_unknown_models_and_attribution_coverage_are_separate(self) -> None:
        self.assert_report("weighted", "weighted")
        self.assertEqual(len(self.rows("SELECT * FROM turns WHERE agent_id = 'report-child'")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 12)

    def test_exact_filters_the_table_and_coverage_to_recorded_evidence(self) -> None:
        self.assert_report("exact", "weighted", "--exact")

    def test_all_dimensions_follow_requested_order_and_compaction_turn_context(self) -> None:
        dimensions = "machine,unit,role,lens,model,effort"
        self.assert_report("dimensions", "weighted", "--by", dimensions)
        self.assert_report("compaction-context", "compactions_per_thread", "--by", dimensions)

    def test_compaction_context_is_correct_without_optional_turn_lookup_indexes(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as database, database:
            database.row_factory = sqlite3.Row
            indexes = cast(list[sqlite3.Row], database.execute("PRAGMA index_list(turns)").fetchall())
            for index in indexes:
                if cast(int, index["unique"]) == 0:
                    name = cast(str, index["name"]).replace('"', '""')
                    _ = database.execute(f'DROP INDEX "{name}"')
        self.assert_report("compaction-context", "compactions_per_thread", "--by",
                           "machine,unit,role,lens,model,effort")

    def test_outcomes_join_on_shared_dimensions_without_multiplying_usage(self) -> None:
        self.assert_report("checkpoint-day-unit", "weighted_per_checkpoint", "--by", "day,unit")
        self.assert_report("finding-day", "weighted_per_finding_closed", "--by", "day")
        self.assert_report("review-day", "weighted_per_review_round", "--by", "day")
        self.assert_report("compaction-day", "compactions_per_thread", "--by", "day")

    def test_unit_is_resolved_from_current_production_docs_with_longest_worktree(self) -> None:
        self.assert_report("unit", "weighted", "--by", "unit")
        document = self.production_docs / "fixture-production.md"
        _ = document.write_text(document.read_text(encoding="utf-8").replace("| Deep |", "| Renamed |"), encoding="utf-8")
        self.assert_report("unit-renamed", "weighted", "--by", "unit")

    def test_unit_containment_does_not_match_a_neighbor_with_the_same_prefix(self) -> None:
        document = self.production_docs / "other-production.md"
        _ = document.write_text("## Units\n\n| Unit | Worktree |\n| --- | --- |\n| Wrong | `/invented/loos` |\n",
                                encoding="utf-8")
        self.assert_report("unit", "weighted", "--by", "unit")

    def test_reports_read_only_store_after_every_source_is_removed(self) -> None:
        before = self.db_path.read_bytes()
        shutil.rmtree(self.claude_root)
        shutil.rmtree(self.codex_root)
        shutil.rmtree(self.history)
        self.agents_path.unlink()
        (self.root / "readings.jsonl").unlink()
        (self.root / "account-roles.jsonl").unlink()
        self.assert_report("weighted", "weighted")
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_no_turns_prints_headers_and_sources_only(self) -> None:
        self.assert_report("no-turns", "weighted", "--since", "2030-01-01", "--until", "2030-01-02")

    def test_account_switch_uses_previous_role_label_before_the_window(self) -> None:
        self.assert_report("account-window", "account_switches", "--by", "day,machine",
                           "--since", "2026-10-11", "--until", "2026-10-12")

    def test_exact_evidence_filter_keeps_account_switch_counts(self) -> None:
        self.assert_report("account-exact", "account_switches", "--by", "day,machine", "--exact")

    def test_account_switches_defaults_to_its_carried_dimensions(self) -> None:
        self.assert_report("account-default", "account_switches")
        result = self.report("account_switches", "--by", "role")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, (FIXTURES / "expected/unsupported-dimension.txt").read_text(encoding="utf-8"))

    def test_partial_mac_is_listed_before_complete_sources_with_kept_turns(self) -> None:
        stream = self.mac_pass("cut_after_first_cursor")
        self.assertEqual(stream["cursor_records"], 1)
        self.assert_report("mac-partial", "weighted", "--by", "machine")

    def test_unreachable_mac_lists_last_complete_time_and_preserves_prior_turns(self) -> None:
        _ = self.mac_pass("complete")
        complete_times = self.mac_complete_times()
        _ = self.mac_pass("unreachable")
        self.assertEqual(self.mac_complete_times(), complete_times)
        self.assert_report("mac-unreachable", "weighted", "--by", "machine")

    def test_skipped_mac_after_full_read_keeps_last_complete_time(self) -> None:
        _ = self.mac_pass("complete")
        complete_times = self.mac_complete_times()
        _ = self.collect()
        self.assertEqual(self.mac_complete_times(), complete_times)
        self.assert_report("mac-skipped", "weighted", "--by", "machine")

    def test_partial_mac_after_full_read_keeps_last_complete_time(self) -> None:
        _ = self.mac_pass("complete")
        complete_times = self.mac_complete_times()
        self.append_bytes(self.root / "fixture-mac-home/.claude/projects/project/report-parent.jsonl", b"\n")
        stream = self.mac_pass("cut_after_first_cursor")
        self.assertEqual(stream["cursor_records"], 1)
        self.assertEqual(self.mac_complete_times(), complete_times)
        self.assert_report("mac-partial-after-full", "weighted", "--by", "machine")

    def test_refusals_name_accepted_choices_on_one_stderr_line(self) -> None:
        cases = (
            ("unknown-metric", ("absent-metric",)),
            ("unknown-dimension", ("weighted", "--by", "absent-dimension")),
            ("unknown-zone", ("weighted", "--zone", "Invented/Zone")),
            ("invalid-date", ("weighted", "--since", "not-a-date")),
            ("unsupported-dimension", ("account_switches", "--by", "role")),
            ("duplicate-dimension", ("weighted", "--by", "role,role")),
            ("invalid-until", ("weighted", "--until", "not-a-date")),
            ("reversed-bounds", ("weighted", "--since", "2026-10-12", "--until", "2026-10-11")),
        )
        for name, arguments in cases:
            with self.subTest(case=name):
                result = self.report(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                expected = (FIXTURES / "expected" / f"{name}.txt").read_text(encoding="utf-8")
                self.assertEqual(result.stderr, expected)
                self.assertEqual(len(result.stderr.splitlines()), 1)

    def test_one_registry_entry_adds_a_named_metric_without_other_changes(self) -> None:
        script = """
import sys
from scripts.usage.usage_report import METRICS, Metric, TurnAggregate, ReportFormat, main
METRICS['turn_count'] = Metric('turn_count', 'Stored turn count',
                             TurnAggregate('turns', 'count(*)'), format=ReportFormat.COUNT)
raise SystemExit(main(sys.argv[1:]))
"""
        result = subprocess.run([sys.executable, "-c", script, "--db", str(self.db_path),
                                 "--production-docs", str(self.production_docs), "turn_count"],
                                env=self.report_environment(), cwd=REPOSITORY, capture_output=True,
                                text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, self.expected_report("turn-count"))

    def test_rate_format_prints_two_decimals_without_a_denominator(self) -> None:
        script = """
import sys
from scripts.usage.usage_report import METRICS, Metric, TurnAggregate, ReportFormat, main
METRICS['input_rate'] = Metric('input_rate', 'Average input divided by three',
                             TurnAggregate('average_input', 'avg(input) / 3.0'), format=ReportFormat.RATE)
raise SystemExit(main(sys.argv[1:]))
"""
        result = self.report_script(script, "input_rate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, self.expected_report("rate-without-denominator"))

    def test_number_format_rounds_a_ratio_without_decimals(self) -> None:
        script = """
import sys
from scripts.usage.usage_report import METRICS, OUTCOMES, Metric, TurnSum, TurnValue, OutcomeCount, ReportFormat, main
METRICS['checkpoint_number'] = Metric('checkpoint_number', 'Weighted checkpoint ratio as a number',
                                    TurnSum('weighted', (TurnValue.WEIGHTED,)),
                                    OutcomeCount(OUTCOMES['checkpoints']), ReportFormat.NUMBER)
raise SystemExit(main(sys.argv[1:]))
"""
        result = self.report_script(script, "checkpoint_number")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, self.expected_report("number-with-denominator"))

    def test_additive_sides_and_general_sql_aggregates_print_the_same_reports(self) -> None:
        script = """
import argparse
import json
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from scripts.usage.usage_report import (
    METRICS, NoDenominator, OutcomeCount, TurnAggregate, TurnSum, TurnValue, main, metric_sides,
)

parser = argparse.ArgumentParser()
parser.add_argument('--db', type=Path, required=True)
parser.add_argument('--production-docs', type=Path, required=True)
paths = parser.parse_args()

weighted = ' SuM ( weighted ) '
general_expressions = {
    'weighted': (weighted,),
    'weighted_per_checkpoint': (weighted,),
    'before_first_edit_share': (
        ' sUm ( cAsE wHeN before_first_edit = 1 tHeN weighted eLsE 0 eNd ) ', weighted,
    ),
    'cache_read_share': (
        ' sUM ( cache_read ) ',
        ' SuM ( input + cache_write_5m + cache_write_1h + cache_read ) ',
    ),
    'output_share': (' SUM ( output_weighted ) ', weighted),
    'weighted_per_finding_closed': (weighted,),
    'weighted_per_review_round': (weighted,),
}
sum_values = {
    'weighted': ((TurnValue.WEIGHTED,),),
    'weighted_per_checkpoint': ((TurnValue.WEIGHTED,),),
    'before_first_edit_share': ((TurnValue.WEIGHTED,), (TurnValue.WEIGHTED,)),
    'cache_read_share': (
        (TurnValue.CACHE_READ,),
        (TurnValue.INPUT, TurnValue.CACHE_WRITE_5M, TurnValue.CACHE_WRITE_1H, TurnValue.CACHE_READ),
    ),
    'output_share': ((TurnValue.OUTPUT_WEIGHTED,), (TurnValue.WEIGHTED,)),
    'weighted_per_finding_closed': ((TurnValue.WEIGHTED,),),
    'weighted_per_review_round': ((TurnValue.WEIGHTED,),),
}

def render(metric, by):
    output = StringIO()
    with redirect_stdout(output):
        result = main(['--db', str(paths.db), '--production-docs', str(paths.production_docs),
                       metric.name, '--by', by])
    assert result == 0, (metric.name, by, result)
    return output.getvalue()

reports = []
for name, expressions in general_expressions.items():
    initial = METRICS[name]
    additive_sides = []
    sides = []
    expression_index = 0
    for side in metric_sides(initial):
        if isinstance(side, OutcomeCount):
            additive_sides.append(side)
            sides.append(side)
            continue
        assert isinstance(side, TurnSum), (name, type(side).__name__)
        expression = expressions[expression_index]
        where = ' EdItEd = 1 ' if name == 'before_first_edit_share' else ' 1 '
        condition = (' BeFoRe_FiRsT_EdIt = 1 '
                     if name == 'before_first_edit_share' and expression_index == 0 else ' 1 ')
        additive_sides.append(TurnSum(side.name, sum_values[name][expression_index],
                                      where=where, condition=condition, show_turns=name == 'weighted'))
        sides.append(TurnAggregate(
            side.name, expression,
            where=where,
            weighted=name != 'cache_read_share',
            show_turns=name == 'weighted',
        ))
        expression_index += 1
    assert expression_index == len(expressions), name
    additive = replace(initial, numerator=additive_sides[0],
                       denominator=additive_sides[1] if len(additive_sides) == 2 else NoDenominator())
    general = replace(initial, numerator=sides[0],
                      denominator=sides[1] if len(sides) == 2 else NoDenominator())
    for by in ('role,model', 'day,machine,unit,role,lens,model,effort'):
        METRICS[name] = additive
        additive_output = render(additive, by)
        METRICS[name] = general
        general_output = render(general, by)
        reports.append({'metric': name, 'by': by,
                        'additive': additive_output, 'general': general_output})
    METRICS[name] = initial
print(json.dumps(reports))
"""
        result = self.report_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        reports = cast(list[EquivalentSideReport], json.loads(result.stdout))
        turn_metrics = tuple(metric for metric in METRICS if metric not in ("compactions_per_thread", "account_switches"))
        self.assertEqual([(row["metric"], row["by"]) for row in reports],
                         [(metric, by) for metric in turn_metrics
                          for by in ("role,model", "day,machine,unit,role,lens,model,effort")])
        for row in reports:
            with self.subTest(metric=row["metric"], by=row["by"]):
                expected_name = row["metric"] if row["by"] == "role,model" else "all-dimensions-" + row["metric"]
                expected = self.expected_report(expected_name)
                self.assertEqual(row["additive"], expected)
                self.assertEqual(row["general"], expected)

    def assert_version_refused_without_writing(self, version: int) -> None:
        with closing(sqlite3.connect(self.db_path)) as database, database:
            if version == 6:
                _ = database.execute("DROP TABLE folders")
            _ = database.execute(f"PRAGMA user_version = {version}")
        before = self.db_path.read_bytes()
        result = self.report("weighted")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        expected = (FIXTURES / "expected" / f"store-version-{version}.txt").read_text(encoding="utf-8")
        self.assertEqual(result.stderr, expected)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_older_store_names_its_version_and_requires_one_collection(self) -> None:
        self.assert_version_refused_without_writing(6)

    def test_newer_store_refuses_an_older_report_without_writing(self) -> None:
        self.assert_version_refused_without_writing(8)

    def test_large_stored_counts_report_raw_and_weighted_sums_without_overflow(self) -> None:
        for directory in (self.claude_root, self.codex_root, self.history):
            shutil.rmtree(directory)
            directory.mkdir(parents=True)
        project = self.claude_root / "project"
        project.mkdir()
        _ = shutil.copyfile(FIXTURES / "large-counts/claude.jsonl", project / "large-count-session.jsonl")
        _ = self.agents_path.write_bytes(b"")
        self.db_path.unlink()
        _ = self.collect()
        self.assertEqual(self.values(self.rows("SELECT input, cache_read, output FROM turns"),
                                     "input", "cache_read", "output"), [(2**62, 1, 1)] * 2)
        for metric in ("cache_read_share", "weighted", "output_share"):
            with self.subTest(metric=metric):
                self.assert_report("large-" + metric, metric, "--by", "model")

    def report_function_calls(self) -> list[ReportFunctionCalls]:
        script = """
import argparse
import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from scripts.usage.usage_report import (
    METRICS, ReportArguments, register_report_functions, render_report, report_request, unit_worktrees,
)

class FunctionCountingConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = Counter()

    def create_function(self, name: str, narg: int, func: Callable[..., object] | None,
                        *, deterministic: bool = False) -> None:
        # None unregisters a function at the sqlite3 API boundary.
        if func is None:
            super().create_function(name, narg, None, deterministic=deterministic)
            return
        self.calls.setdefault(name, 0)
        def counted(*values: object) -> object:
            self.calls[name] += 1
            return func(*values)
        super().create_function(name, narg, counted, deterministic=deterministic)

    def create_aggregate(self, name, narg, aggregate_class):
        if aggregate_class is None:
            super().create_aggregate(name, narg, None)
            return
        calls = self.calls
        class CountedAggregate:
            def __init__(self):
                self.aggregate = aggregate_class()
            def step(self, *values):
                calls[name + '.step'] += 1
                return self.aggregate.step(*values)
            def finalize(self):
                calls[name + '.finalize'] += 1
                return self.aggregate.finalize()
        super().create_aggregate(name, narg, CountedAggregate)

parser = argparse.ArgumentParser()
parser.add_argument('--db', type=Path, required=True)
parser.add_argument('--production-docs', type=Path, required=True)
paths = parser.parse_args()
results = []
for metric in METRICS:
    groupings = ('day,machine', 'day') if metric == 'account_switches' else (
        'role,model', 'day,model', 'day,role,lens,model,effort,unit,machine',
    )
    for by in groupings:
        args = ReportArguments()
        args.metric = metric
        args.by = by
        args.db = paths.db
        args.production_docs = paths.production_docs
        request = report_request(args)
        with FunctionCountingConnection(paths.db.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            register_report_functions(db, unit_worktrees(paths.production_docs))
            text = render_report(db, request)
            assert text.startswith(metric + ' — ')
            results.append({'metric': metric, 'by': by, 'calls': dict(db.calls)})
        db.close()
print(json.dumps(results))
"""
        result = self.report_script(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return cast(list[ReportFunctionCalls], json.loads(result.stdout))

    def test_python_function_calls_depend_on_groups_folders_and_compactions_not_turn_count(self) -> None:
        baseline = self.report_function_calls()
        folders = self.all_values(self.rows("SELECT * FROM folders ORDER BY family, agent_id"))
        compactions = self.all_values(self.rows("SELECT * FROM compactions ORDER BY family, agent_id, at"))
        hours = self.values(self.rows("SELECT DISTINCT substr(at, 1, 13) AS hour FROM turns ORDER BY hour"), "hour")
        group_query = ("SELECT DISTINCT machine, family, parent_id, agent_id, substr(at, 1, 13), model, effort, "
                       "role, lens, lens_state, attribution, before_first_edit FROM turns")
        groups = sorted(self.all_values(self.rows(group_query)), key=repr)
        for root in (self.claude_root, self.codex_root):
            for path in root.rglob("*.jsonl"):
                expanded: list[bytes] = []
                for line in path.read_bytes().splitlines(keepends=True):
                    expanded.append(line)
                    record = cast(dict[str, object], json.loads(line))
                    if root == self.claude_root:
                        message = record.get("message")
                        if not isinstance(message, dict):
                            continue
                        fields = cast(dict[str, object], message)
                        if not isinstance(fields.get("usage"), dict):
                            continue
                        for copy in range(1, 4):
                            clone = {**record, "message": {**fields, "id": f"{fields['id']}-copy-{copy}"},
                                     "requestId": f"{record['requestId']}-copy-{copy}"}
                            expanded.append(json.dumps(clone).encode("utf-8") + b"\n")
                    else:
                        payload = record.get("payload")
                        if isinstance(payload, dict) and cast(dict[str, object], payload).get("type") == "token_count":
                            expanded.extend([line] * 3)
                _ = path.write_bytes(b"".join(expanded))
        self.db_path.unlink()
        _ = self.collect()
        self.assertEqual(len(self.rows("SELECT * FROM turns")), 48)
        self.assertEqual(sorted(self.all_values(self.rows(group_query)), key=repr), groups)
        self.assertEqual(self.all_values(self.rows("SELECT * FROM folders ORDER BY family, agent_id")), folders)
        self.assertEqual(self.all_values(self.rows("SELECT * FROM compactions ORDER BY family, agent_id, at")), compactions)
        self.assertEqual(self.values(self.rows("SELECT DISTINCT substr(at, 1, 13) AS hour FROM turns ORDER BY hour"), "hour"),
                         hours)
        expanded_calls = self.report_function_calls()
        self.assertEqual([(row["metric"], row["by"]) for row in expanded_calls],
                         [(row["metric"], row["by"]) for row in baseline])
        for small, large in zip(baseline, expanded_calls, strict=True):
            for function in small["calls"].keys() | large["calls"].keys():
                with self.subTest(metric=small["metric"], by=small["by"], function=function):
                    self.assertLessEqual(large["calls"].get(function, 0), small["calls"].get(function, 0))

    def test_missing_database_is_not_created(self) -> None:
        missing = self.state / "missing.db"
        self.db_path = missing
        result = self.report("weighted")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    _ = unittest.main()
