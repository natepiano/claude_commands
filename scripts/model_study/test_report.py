"""Synthetic tests for the director study report and sample gate."""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch
from zoneinfo import ZoneInfo

import compare
import model_study
from compare import (
    ArmCounts,
    ComparisonReport,
    CompactionComparison,
    ConcurrentControl,
    ControlDirector,
    DifferenceEstimate,
    DirectorComparison,
    DirectorStatus,
    DropSummary,
    TurnClassStatistics,
)
from phases import PhaseComparison, PhaseDifference, PhaseReport, PhaseRow
from report import ExtractReport, ExtractSession, SAMPLE_DEADLINE_PDT, message, ready, recommendation_line, render, verdict


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
SECRET = "SECRET_TRANSCRIPT_PROMPT_AND_PATH"
CLASS_NAMES = ("continuation", "continuation/tool_use", "continuation/end_turn", "prompt")


def class_statistics(n: int, seconds: float | None, output: float | None, cost: float | None) -> TurnClassStatistics:
    return {
        "n": n, "timed_n": n if seconds is not None else 0,
        "seconds_median": seconds, "seconds_p25": seconds,
        "seconds_p75": seconds, "seconds_p90": seconds,
        "output_median": output, "thinking_median": 10 if n else None,
        "fresh_input_median": 20 if n else None,
        "cache_read_median": 30 if n else None,
        "context_median": 50 if n else None,
        "output_per_second_median": 5 if seconds is not None else None,
        "cost_mean": cost,
    }


def arm_classes(n: int, seconds: float | None, output: float | None, cost: float | None) -> dict[str, TurnClassStatistics]:
    return {name: class_statistics(n if name == "continuation" else 0,
                                   seconds if name == "continuation" else None,
                                   output if name == "continuation" else None,
                                   cost if name == "continuation" else None)
            for name in CLASS_NAMES}


def estimate(label: str, value: float | None, opus_n: int = 40, sonnet_n: int = 50) -> DifferenceEstimate:
    return {"value": value, "low": value, "high": value, "label": label,
            "opus_n": opus_n, "sonnet_n": sonnet_n}


def drop_summary(value: int = 0) -> DropSummary:
    return {"switch_turn": value, "effort": value, "speed": value}


def director(
    name: str, status: DirectorStatus, opus_n: int, sonnet_n: int,
    opus_seconds: float | None, sonnet_seconds: float | None,
) -> DirectorComparison:
    counts: ArmCounts = {
        "opus_all": opus_n, "opus_baseline": opus_n,
        "sonnet": sonnet_n, "other": 2 if status == "no eligible requests" else 0,
    }
    classes = {} if status == "no eligible requests" else {
        "opus_all": arm_classes(opus_n, opus_seconds, 120 if opus_n else None, 0.02 if opus_n else None),
        "opus": arm_classes(opus_n, opus_seconds, 120 if opus_n else None, 0.02 if opus_n else None),
        "sonnet": arm_classes(sonnet_n, sonnet_seconds, 80 if sonnet_n else None, 0.01 if sonnet_n else None),
    }
    differences = None if status not in ("switched", "pooled") else {
        "seconds": estimate("no measurable difference", sonnet_seconds - opus_seconds
                            if opus_seconds is not None and sonnet_seconds is not None else None),
        "output": estimate("no measurable difference", -40),
        "cost": estimate("no measurable difference", -0.01),
    }
    return {
        "name": name, "status": status,
        "first_sonnet_pdt": "2026-10-06 09:30 PDT" if sonnet_n else None,
        "counts": counts,
        "drops": {"opus": drop_summary(1), "sonnet": drop_summary(2),
                  "other": drop_summary(3)},
        "classes": classes, "differences": differences,
    }


def compaction(name: str, arm: str, rate: float | None) -> CompactionComparison:
    return {
        "name": name, "arm": arm, "requests": 40, "compactions": 1,
        "active_hours": 2, "per_100_requests": 2.5,
        "per_active_hour": 0.5, "pre_tokens_median": 5000,
        "duration_ms_median": 1000 if rate is not None else None,
        "seconds_per_active_hour": rate,
        "post_requests": 4, "other_requests": 36,
        "post_seconds_ratio": 1.2, "post_write_ratio": 1.1,
    }


def comparison_fixture() -> ComparisonReport:
    alpha = director("alpha", "switched", 40, 50, 20, 10)
    beta = director("beta", "switched", 40, 50, None, 8)
    pooled = director("Pooled switched", "pooled", 80, 100, 20, 10)
    control: ConcurrentControl = {"at_pdt": "2026-10-06 09:30 PDT", "directors": []}
    return {
        "baseline_requests": 300, "prices_usd_per_million": {},
        "directors": [alpha, beta, director("gamma", "Sonnet-only", 0, 30, None, 9),
                      director("empty", "no eligible requests", 0, 0, None, None)],
        "pooled": pooled, "control": control,
        "compactions": [
            compaction("alpha", "opus", 1), compaction("alpha", "sonnet", 2),
            compaction("beta", "opus", 1), compaction("beta", "sonnet", None),
        ],
    }


def phase_difference(label: str, opus_n: int = 2, sonnet_n: int = 3) -> PhaseDifference:
    return {
        "opus_n": opus_n, "sonnet_n": sonnet_n,
        "opus_median": 2, "sonnet_median": 3,
        "value": None, "low": None, "high": None, "label": label,
    }


def phase_comparison(name: str) -> PhaseComparison:
    label = "too few phases (n=2 against 3)"
    return {"director": name, "metrics": {
        metric: phase_difference(label) for metric in
        ("requests_per_1000_words", "seconds_per_1000_words",
         "cost_per_1000_words", "repair_rounds")
    }}


def phase_fixture() -> PhaseReport:
    row: PhaseRow = {
        "director": "alpha", "plan": "synthetic", "phase_id": "safe-id",
        "phase_title": "Safe title", "arm": "opus", "requests": 4,
        "director_seconds": 80, "cost_usd": 0.10,
        "output_tokens": 200, "repair_rounds": 2,
        "findings_opened": 3, "abandoned_batches": 1,
        "phase_elapsed_seconds": 200, "work_order_words": None,
        "work_order_lines": None, "requests_per_1000_words": None,
        "seconds_per_1000_words": None, "cost_per_1000_words": None,
    }
    return {
        "phases": [row],
        "comparisons": [phase_comparison("alpha"), phase_comparison("beta"),
                        phase_comparison("Pooled switched")],
        "drops": {"stopped": 5, "errored": 2, "empty": 75},
    }


def extract_fixture() -> ExtractReport:
    dropped = {"before_director": 1, "sidechain": 2, "no_trigger": 3,
               "negative": 4, "over_limit": 5, "synthetic": 6}
    sessions: list[ExtractSession] = [
        {"name": name, "requests": 100, "dropped": dropped}
        for name in ("alpha", "beta", "gamma", "empty")
    ]
    cast(dict[str, object], cast(object, sessions[0]))["prompt_text"] = SECRET
    return {"sessions": sessions}


class ReportTests(unittest.TestCase):
    def test_cost_alone_can_recommend_sonnet(self) -> None:
        comparison = comparison_fixture()
        phases = phase_fixture()
        pooled = comparison["pooled"]
        assert pooled is not None
        differences = pooled["differences"]
        assert differences is not None
        differences["seconds"]["label"] = "too few"
        differences["cost"]["label"] = "lower"

        self.assertEqual(verdict(comparison, phases), "sonnet")
        self.assertEqual(recommendation_line(comparison, phases),
                         "Recommendation: sonnet — cost per request is lower.")

        differences["cost"]["label"] = "too few"
        self.assertEqual(verdict(comparison, phases),
                         "sonnet stays (no measurable difference; evidence thin)")

    def test_verdict_rule_covers_every_branch(self) -> None:
        comparison = comparison_fixture()
        phases = phase_fixture()
        pooled = comparison["pooled"]
        assert pooled is not None
        differences = pooled["differences"]
        assert differences is not None
        work = phases["comparisons"][-1]["metrics"]

        differences["seconds"]["label"] = "slower"
        differences["cost"]["label"] = "lower"
        self.assertEqual(verdict(comparison, phases), "opus")
        self.assertIn("continuation time", recommendation_line(comparison, phases))

        differences["seconds"]["label"] = "too few"
        work["requests_per_1000_words"]["label"] = "more"
        self.assertEqual(verdict(comparison, phases), "opus")
        self.assertIn("requests per 1,000", recommendation_line(comparison, phases))

        work["requests_per_1000_words"]["label"] = "too few phases (n=2 against 3)"
        work["repair_rounds"]["label"] = "more"
        differences["seconds"]["label"] = "faster"
        self.assertEqual(verdict(comparison, phases), "opus")
        self.assertIn("repair rounds", recommendation_line(comparison, phases))

        work["repair_rounds"]["label"] = "too few phases (n=2 against 3)"
        self.assertEqual(verdict(comparison, phases), "sonnet")

        differences["cost"]["label"] = "too few"
        differences["seconds"]["label"] = "faster"
        self.assertEqual(verdict(comparison, phases), "sonnet")

        differences["seconds"]["label"] = "too few"
        self.assertEqual(verdict(comparison, phases), "sonnet stays (no measurable difference; evidence thin)")

    def test_report_orders_measure_before_verdict_and_shows_limits(self) -> None:
        comparison = comparison_fixture()
        phases = phase_fixture()
        extracted = extract_fixture()
        document = render(comparison, phases, extracted, NOW)
        headings = [
            "## The user's measure", "## Verdict", "## Per-director comparison",
            "## Pooled turn classes", "## Control", "## Compactions",
            "## Work per phase", "## Limits",
        ]
        self.assertTrue(document.startswith("# INTERIM director model study — 2026-10-06 12:00 PDT"))
        self.assertEqual([document.index(heading) for heading in headings],
                         sorted(document.index(heading) for heading in headings))
        measure = document.split("## The user's measure\n\n", 1)[1].split("\n\n## Verdict", 1)[0].splitlines()
        self.assertEqual(len(measure), 3)
        self.assertIn("alpha: median continuation-turn seconds Opus 20.00 → Sonnet 10.00 (-50.0%)", measure)
        self.assertIn("beta: median continuation-turn seconds Opus n/a → Sonnet 8.00", measure)
        self.assertNotIn("%", next(line for line in measure if line.startswith("beta:")))
        self.assertTrue(measure[-1].startswith("Pooled switched: median continuation-turn seconds"))
        self.assertIn("work: requests per 1,000 Work Order words too few phases (n=2 against 3)", document)
        self.assertIn("Default for enh-showrunner Phase 6: sonnet", document)
        self.assertIn("no control candidate qualified (30 filtered Opus continuation requests on each side of T)", document)
        self.assertIn("compaction: Sonnet / Opus —", document)
        self.assertIn("| beta | sonnet | 1 / 40 | 2.50 | 0.50 | — | — |", document)
        self.assertIn("| alpha | opus | 4 | 80.00 | 0.10000 | 200 | 2 | 3 | 1 | 200.00 | — / — | — | — | — |", document)
        self.assertIn("empty has no eligible requests", document)
        self.assertIn("gamma is Sonnet-only; there is no Opus baseline", document)
        self.assertIn("before_director 1", document)
        self.assertIn("synthetic 6", document)
        self.assertIn("Phase drops: stopped 5, errored 2, no request 75", document)
        self.assertIn("Compaction duration unknown", document)
        self.assertNotIn(SECRET, document)

    def test_work_comparisons_follow_phase_entries_even_for_sonnet_only_director(self) -> None:
        comparison = comparison_fixture()
        phases = phase_fixture()
        phases["comparisons"].insert(1, phase_comparison("gamma"))

        document = render(comparison, phases, extract_fixture(), NOW)
        work = document.split("## Work per phase\n\n", 1)[1].split("\n\n## Limits", 1)[0]
        comparison_table = work.split("\n\n| Director | Arm |", 1)[0]
        director_lines = [line for line in comparison_table.splitlines()
                          if line.startswith("| ") and not line.startswith(("| Director", "| ---"))]
        self.assertEqual([line.split("|")[1].strip() for line in director_lines],
                         ["alpha", "gamma", "beta"])
        self.assertNotIn("| Pooled switched |", comparison_table)

    def test_zero_work_order_size_renders_as_missing(self) -> None:
        phases = phase_fixture()
        phases["phases"][0]["work_order_words"] = 0
        phases["phases"][0]["work_order_lines"] = 0

        document = render(comparison_fixture(), phases, extract_fixture(), NOW)
        self.assertIn("| alpha | opus | 4 | 80.00 | 0.10000 | 200 | 2 | 3 | 1 | 200.00 | — / — |", document)

    def test_limits_include_extracted_session_without_comparison(self) -> None:
        extracted = extract_fixture()
        extracted["sessions"].append({
            "name": "orphan", "requests": 0,
            "dropped": {"before_director": 1, "sidechain": 2, "no_trigger": 3,
                        "negative": 4, "over_limit": 5, "synthetic": 6},
        })

        document = render(comparison_fixture(), phase_fixture(), extracted, NOW)
        limits = document.split("## Limits\n\n", 1)[1]
        self.assertIn("orphan: no requests extracted.", limits)
        self.assertIn("orphan extract drops: before_director 1, sidechain 2, no_trigger 3, negative 4, over_limit 5, synthetic 6.", limits)

    def test_measure_percent_uses_medians_when_interval_is_too_few(self) -> None:
        comparison = comparison_fixture()
        alpha = comparison["directors"][0]
        differences = alpha["differences"]
        assert differences is not None
        differences["seconds"]["label"] = "too few"
        differences["seconds"]["value"] = None

        document = render(comparison, phase_fixture(), extract_fixture(), NOW)
        measure = document.split("## The user's measure\n\n", 1)[1].split("\n\n## Verdict", 1)[0]
        self.assertIn("alpha: median continuation-turn seconds Opus 20.00 → Sonnet 10.00 (-50.0%)", measure)

        alpha["classes"]["opus"]["continuation"] = class_statistics(0, None, None, None)
        document = render(comparison, phase_fixture(), extract_fixture(), NOW)
        measure = document.split("## The user's measure\n\n", 1)[1].split("\n\n## Verdict", 1)[0]
        alpha_line = next(line for line in measure.splitlines() if line.startswith("alpha:"))
        self.assertEqual(alpha_line, "alpha: median continuation-turn seconds Opus n/a → Sonnet 10.00")
        self.assertNotIn("%", alpha_line)

    def test_null_control_median_is_dash(self) -> None:
        comparison = comparison_fixture()
        control: ControlDirector = {
            "name": "control", "before_n": 30, "after_n": 30,
            "before_median": None, "after_median": 25,
            "difference": estimate("too few", None, 0, 30),
        }
        comparison["control"]["directors"].append(control)
        document = render(comparison, phase_fixture(), extract_fixture(), NOW)
        self.assertIn("| control | 30 / — | 30 / 25.00 | — (too few) |", document)

    def test_compaction_verdict_weights_director_rates_by_active_hours(self) -> None:
        comparison = comparison_fixture()
        sonnet = next(row for row in comparison["compactions"]
                      if row["name"] == "beta" and row["arm"] == "sonnet")
        sonnet["seconds_per_active_hour"] = 3
        sonnet["active_hours"] = 6
        document = render(comparison, phase_fixture(), extract_fixture(), NOW)
        self.assertIn("compaction: Sonnet / Opus 2.75× (Opus 1.00 s/active h; Sonnet 2.75 s/active h)", document)

    def test_short_message_starts_with_pinned_line_and_measure(self) -> None:
        comparison = comparison_fixture()
        phases = phase_fixture()
        extracted = extract_fixture()
        path = Path("/tmp/synthetic-state/report.md")
        output = message(comparison, phases, extracted, NOW, False, path)
        lines = output.splitlines()
        self.assertEqual(lines[0],
                         "From model-study-unit: INTERIM director model study — sonnet stays (no measurable difference; evidence thin).")
        self.assertLessEqual(len(lines), 60)
        self.assertTrue(lines[1].startswith("alpha: median continuation-turn seconds"))
        self.assertTrue(lines[2].startswith("beta: median continuation-turn seconds Opus n/a"))
        self.assertTrue(lines[3].startswith("Pooled switched: median continuation-turn seconds"))
        self.assertEqual(lines[4], "Verdict:")
        self.assertEqual(lines[-1], str(path))
        self.assertNotIn(SECRET, output)

    def test_short_message_caps_director_rows_and_counts_omitted_directors(self) -> None:
        comparison = comparison_fixture()
        names = [f"director_{index:02d}" for index in range(24)]
        comparison["directors"] = [director(name, "switched", 40, 50, 20, 10) for name in names]
        path = Path("/tmp/synthetic-state/report.md")

        output = message(comparison, phase_fixture(), extract_fixture(), NOW, False, path)
        lines = output.splitlines()
        self.assertLessEqual(len(lines), 60)
        self.assertEqual(lines[0],
                         "From model-study-unit: INTERIM director model study — sonnet stays (no measurable difference; evidence thin).")
        for name in names:
            self.assertTrue(any(line.startswith(f"{name}: median continuation-turn seconds") for line in lines))
        self.assertIn("Verdict:", lines)
        self.assertIn("Default for enh-showrunner Phase 6: sonnet", lines)
        self.assertTrue(any(line.startswith("Limits: ") for line in lines))
        self.assertEqual(lines[-1], str(path))
        table_start = lines.index("Per-director comparison:")
        table_end = next(index for index, line in enumerate(lines) if line.startswith("Limits: "))
        table = lines[table_start:table_end]
        included = [line.split("|")[1].strip() for line in table
                    if line.startswith("| director_")]
        omitted = names[len(included):]
        self.assertGreater(len(omitted), 0)
        self.assertEqual(included, names[:len(included)])
        self.assertEqual(table[-1], f"… {len(omitted)} more directors in report.md")
        for name in omitted:
            self.assertFalse(any(line.startswith(f"| {name} |") for line in table))

    def test_ready_uses_filtered_continuation_counts_and_deadline(self) -> None:
        comparison = comparison_fixture()
        before = datetime(2026, 10, 7, 10, 29, tzinfo=ZoneInfo("America/Los_Angeles"))
        gate, lines = ready(comparison, before)
        self.assertFalse(gate)
        self.assertEqual(sum("filtered Sonnet continuation requests" in line for line in lines), 2)
        self.assertIn("Deadline: 2026-10-07 10:30 PDT", lines)

        four = copy.deepcopy(comparison)
        four["directors"] = [director(name, "switched", 40, 150, 20, 10)
                             for name in ("alpha", "beta", "delta", "epsilon")]
        for row in four["directors"]:
            row["classes"]["sonnet"]["continuation"]["n"] = 150
        gate, lines = ready(four, before)
        self.assertTrue(gate)
        self.assertEqual(sum("150 filtered Sonnet continuation requests" in line for line in lines), 4)

        short = copy.deepcopy(four)
        short["directors"][0]["classes"]["sonnet"]["continuation"]["n"] = 149
        self.assertFalse(ready(short, before)[0])
        self.assertTrue(ready(short, SAMPLE_DEADLINE_PDT)[0])

    def test_report_message_cli_writes_report_without_comparison_tables(self) -> None:
        comparison = comparison_fixture()
        phases = phase_fixture()
        extracted = extract_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            _ = (state_dir / "extract.json").write_text(json.dumps(extracted), encoding="utf-8")
            args = ["model_study.py", "report", "--interim", "--message", "--no-extract",
                    "--state-dir", str(state_dir), "--now", NOW.isoformat()]
            output = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(model_study, "extract") as extraction, \
                 patch.object(model_study, "compare", return_value=comparison), \
                 patch.object(model_study, "phases", return_value=phases), redirect_stdout(output):
                model_study.main()
            extraction.assert_not_called()
            lines = output.getvalue().splitlines()
            self.assertLessEqual(len(lines), 60)
            self.assertEqual(lines[0],
                             "From model-study-unit: INTERIM director model study — sonnet stays (no measurable difference; evidence thin).")
            self.assertEqual(lines[-1], str(state_dir / "report.md"))
            self.assertNotIn("# Director model comparison", output.getvalue())
            self.assertNotIn(SECRET, output.getvalue())
            document = (state_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("## The user's measure", document)
            self.assertNotIn(SECRET, document)

    def test_ready_cli_exits_three_until_count_or_clock_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            comparison = comparison_fixture()
            before = datetime(2026, 10, 7, 10, 29, tzinfo=ZoneInfo("America/Los_Angeles"))

            def run_gate(data: ComparisonReport, at: datetime) -> tuple[int, str]:
                args = ["model_study.py", "ready", "--state-dir", str(state_dir),
                        "--now", at.isoformat()]
                output = io.StringIO()
                with patch.object(sys, "argv", args), \
                     patch.object(model_study, "extract", return_value=extract_fixture()), \
                     patch.object(model_study, "compare", return_value=data), redirect_stdout(output):
                    try:
                        model_study.main()
                    except SystemExit as stopped:
                        return cast(int, stopped.code), output.getvalue()
                return 0, output.getvalue()

            status, output = run_gate(comparison, before)
            self.assertEqual(status, 3)
            self.assertIn("alpha: 50 filtered Sonnet continuation requests", output)
            self.assertIn("beta: 50 filtered Sonnet continuation requests", output)
            self.assertNotIn("gamma: 30 filtered Sonnet continuation requests", output)

            four = copy.deepcopy(comparison)
            four["directors"] = [director(name, "switched", 40, 150, 20, 10)
                                 for name in ("alpha", "beta", "delta", "epsilon")]
            status, output = run_gate(four, before)
            self.assertEqual(status, 0)
            self.assertEqual(output.count("150 filtered Sonnet continuation requests"), 4)

            status, output = run_gate(comparison, SAMPLE_DEADLINE_PDT)
            self.assertEqual(status, 0)
            self.assertIn("deadline passed", output)

    def test_compare_function_does_not_print_its_tables_for_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            _ = (state_dir / "turns.jsonl").write_text("", encoding="utf-8")
            _ = (state_dir / "compactions.jsonl").write_text("", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                result = compare.compare(state_dir)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(result["directors"], [])
            self.assertTrue((state_dir / "compare.json").exists())
