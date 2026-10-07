"""Synthetic request-row tests for the Opus and Sonnet comparison."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from turns import Compaction, Turn


SCRIPT = Path(__file__).with_name("model_study.py")
START = datetime(2026, 10, 6, tzinfo=timezone.utc)
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
type JsonRecord = dict[str, object]


def record(parent: JsonRecord, key: str) -> JsonRecord:
    value = parent[key]
    assert isinstance(value, dict)
    return cast(JsonRecord, value)


def records(parent: JsonRecord, key: str) -> list[JsonRecord]:
    value = parent[key]
    assert isinstance(value, list)
    return cast(list[JsonRecord], value)


def named(rows: list[JsonRecord], name: str) -> JsonRecord:
    return next(row for row in rows if row["name"] == name)


def instant(minutes: int, seconds: int = 0) -> str:
    return (START + timedelta(minutes=minutes, seconds=seconds)).isoformat()


def turn(
    name: str,
    model: str,
    minute: int,
    *,
    seconds: float | None,
    output: int = 100,
    write: int = 10,
    kind: str = "continuation",
    stop: str = "tool_use",
    switch_turn: bool = False,
    effort: str = "xhigh",
    speed: str = "standard",
    after_compact: int = 0,
) -> Turn:
    return Turn(
        session=f"{name}-session",
        name=name,
        request_id=f"{name}-{model}-{minute}-{kind}",
        started=instant(minute),
        ended=instant(minute, int(seconds or 0)),
        seconds=seconds,
        model=model,
        effort=effort,
        speed=speed,
        stop=stop,
        kind=kind,
        input=40,
        output=output,
        thinking=20,
        cache_read=30,
        write_5m=write,
        write_1h=0,
        context=80,
        switch_turn=switch_turn,
        after_compact=after_compact,
    )


def fixture_rows() -> tuple[list[Turn], list[Compaction]]:
    turns = [
        turn("switched", OPUS, minute, seconds=100 if 1 <= minute <= 5 else 30,
             output=1000 if 1 <= minute <= 5 else 100, switch_turn=minute == 0,
             stop="tool_use" if minute % 2 == 0 else "end_turn")
        for minute in range(306)
    ]
    turns[0] = replace(turns[0], request_id="SECRET_REQUEST_ID")
    turns.extend(
        turn("switched", SONNET, 360 + index, seconds=20 if 1 <= index <= 10 else 10,
             output=80, write=20 if 1 <= index <= 10 else 10,
             switch_turn=index == 0, after_compact=index if 1 <= index <= 11 else 0,
             stop="tool_use" if index % 2 == 0 else "end_turn")
        for index in range(41)
    )
    turns.append(turn("switched", SONNET, 401, seconds=None, output=80,
                      kind="human", stop="end_turn"))
    turns.extend([
        turn("switched", OPUS, 330, seconds=30, effort="high"),
        turn("switched", OPUS, 331, seconds=30, speed="fast"),
        turn("switched", SONNET, 402, seconds=10, effort="high"),
        turn("switched", SONNET, 403, seconds=10, speed="fast"),
        turn("switched", "claude-haiku-5-5", 404, seconds=5),
    ])
    turns.extend(
        turn("control", OPUS, minute, seconds=25, switch_turn=minute == 0)
        for minute in range(31)
    )
    turns.extend(turn("control", OPUS, 420 + index, seconds=25) for index in range(30))
    turns.extend([
        turn("sonnet-only", SONNET, 500, seconds=10, switch_turn=True),
        turn("sonnet-only", SONNET, 501, seconds=10),
        turn("sonnet-only", SONNET, 502, seconds=None, kind="peer", stop="end_turn"),
        turn("ineligible", "claude-haiku-5-5", 503, seconds=5),
        turn("ineligible", OPUS, 504, seconds=5, switch_turn=True),
        turn("ineligible", SONNET, 505, seconds=5, switch_turn=True),
    ])
    compactions = [
        Compaction("switched-session", "switched", instant(300, 30), "auto", 1000, 200, 500, OPUS),
        Compaction("switched-session", "switched", instant(360, 30), "auto", 2000, 300, 1000, SONNET),
    ]
    return turns, compactions


def write_fixture(
    state_dir: Path, turns: list[Turn] | None = None, compactions: list[Compaction] | None = None,
) -> None:
    fixture_turns, fixture_compactions = fixture_rows()
    turns = turns if turns is not None else fixture_turns
    compactions = compactions if compactions is not None else fixture_compactions
    _ = (state_dir / "turns.jsonl").write_text(
        "".join(json.dumps(row.to_json()) + "\n" for row in turns), encoding="utf-8"
    )
    _ = (state_dir / "compactions.jsonl").write_text(
        "".join(json.dumps(row.to_json()) + "\n" for row in compactions), encoding="utf-8"
    )
    _ = (state_dir / "extract.json").write_text(json.dumps({
        "sessions": [{"name": "switched", "first_sonnet": instant(360)}],
    }), encoding="utf-8")


def run_compare(state_dir: Path) -> tuple[JsonRecord, str, str]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "compare", "--state-dir", str(state_dir)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    output = (state_dir / "compare.json").read_text(encoding="utf-8")
    return cast(JsonRecord, json.loads(output)), completed.stdout, output


class ComparisonTests(unittest.TestCase):
    def test_compare_cli_writes_report_and_machine_readable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            write_fixture(state_dir)
            result, report, output = run_compare(state_dir)
            self.assertIn("switched", report)
            self.assertIn("sonnet-only", report)
            self.assertIn("control", report)
            directors = records(result, "directors")
            switched = named(directors, "switched")
            sonnet_only = named(directors, "sonnet-only")
            counts = record(switched, "counts")
            self.assertEqual(switched["status"], "switched")
            self.assertEqual(counts, {
                "opus_all": 305, "opus_baseline": 300, "sonnet": 41, "other": 1,
            })
            self.assertEqual(sonnet_only["status"], "Sonnet-only")
            self.assertEqual(record(sonnet_only, "counts")["opus_baseline"], 0)
            self.assertEqual(record(sonnet_only, "counts")["sonnet"], 2)
            self.assertIsNone(sonnet_only["differences"])

            drops = record(switched, "drops")
            self.assertEqual(record(drops, "opus"), {
                "switch_turn": 1, "effort": 1, "speed": 1,
            })
            self.assertEqual(record(drops, "sonnet"), {
                "switch_turn": 1, "effort": 1, "speed": 1,
            })
            self.assertEqual(record(drops, "other")["switch_turn"], 0)
            self.assertEqual(record(record(named(directors, "control"), "drops"), "opus")["switch_turn"], 1)

            classes = record(switched, "classes")
            opus_all = record(classes, "opus_all")
            baseline = record(classes, "opus")
            sonnet = record(classes, "sonnet")
            self.assertEqual(record(opus_all, "continuation")["n"], 305)
            self.assertEqual(record(baseline, "continuation")["n"], 300)
            self.assertGreater(
                cast(float, record(opus_all, "continuation")["cost_mean"]),
                cast(float, record(baseline, "continuation")["cost_mean"]),
            )
            self.assertAlmostEqual(cast(float, record(baseline, "continuation")["cost_mean"]), 0.002216)
            self.assertEqual(record(baseline, "continuation/tool_use")["n"], 150)
            self.assertEqual(record(baseline, "continuation/end_turn")["n"], 150)
            self.assertEqual(record(sonnet, "continuation/tool_use")["n"], 20)
            self.assertEqual(record(sonnet, "continuation/end_turn")["n"], 20)
            self.assertEqual(record(sonnet, "continuation")["seconds_median"], 10)
            self.assertEqual(record(sonnet, "continuation")["seconds_p25"], 10)
            self.assertEqual(record(sonnet, "continuation")["seconds_p75"], 12.5)
            self.assertEqual(record(sonnet, "continuation")["seconds_p90"], 20)
            self.assertEqual(record(sonnet, "continuation")["thinking_median"], 20)
            self.assertEqual(record(sonnet, "continuation")["fresh_input_median"], 50)
            self.assertEqual(record(sonnet, "continuation")["cache_read_median"], 30)
            self.assertEqual(record(sonnet, "continuation")["context_median"], 80)
            self.assertEqual(record(sonnet, "continuation")["output_per_second_median"], 8)
            self.assertEqual(record(sonnet, "prompt")["n"], 1)
            self.assertEqual(record(sonnet, "prompt")["timed_n"], 0)
            self.assertIsNone(record(sonnet, "prompt")["seconds_median"])
            self.assertEqual(record(sonnet, "prompt")["output_median"], 80)

            differences = record(switched, "differences")
            self.assertEqual(record(differences, "seconds")["value"], -20)
            self.assertEqual(record(differences, "seconds")["label"], "faster")
            self.assertEqual(record(differences, "net_seconds")["value"], -20)
            self.assertEqual(record(differences, "net_seconds")["label"], "faster")
            self.assertEqual(record(differences, "output")["value"], -20)
            self.assertEqual(record(differences, "output")["label"], "lower")
            self.assertEqual(record(differences, "cost")["label"], "lower")
            self.assertAlmostEqual(cast(float, record(differences, "cost")["value"]), -0.001298902439)
            pooled = record(result, "pooled")
            self.assertEqual(record(pooled, "counts")["opus_baseline"], 300)
            self.assertEqual(record(record(pooled, "differences"), "seconds")["value"], -20)
            self.assertEqual(record(record(pooled, "differences"), "net_seconds")["value"], -20)

            control = record(result, "control")
            self.assertEqual(control["at_pdt"], "2026-10-05 23:00 PDT")
            stable = named(records(control, "directors"), "control")
            self.assertEqual((stable["before_n"], stable["after_n"]), (30, 30))
            self.assertEqual((stable["before_median"], stable["after_median"]), (25, 25))
            self.assertEqual(record(stable, "difference")["value"], 0)
            self.assertEqual(record(stable, "difference")["label"], "no measurable difference")
            self.assertEqual(record(control, "pooled"), {
                "before_n": 30, "after_n": 30, "before_median": 25,
                "after_median": 25, "change_seconds": 0,
            })

            compactions = records(result, "compactions")
            opus_compact = next(row for row in compactions if row["name"] == "switched" and row["arm"] == "opus")
            sonnet_compact = next(row for row in compactions if row["name"] == "switched" and row["arm"] == "sonnet")
            self.assertEqual(opus_compact["pre_tokens_median"], 1000)
            self.assertEqual(opus_compact["duration_ms_median"], 500)
            self.assertAlmostEqual(cast(float, opus_compact["per_100_requests"]), 100 / 305)
            self.assertAlmostEqual(cast(float, opus_compact["per_active_hour"]), 60 / 304)
            self.assertEqual(sonnet_compact["pre_tokens_median"], 2000)
            self.assertEqual(sonnet_compact["duration_ms_median"], 1000)
            self.assertAlmostEqual(cast(float, sonnet_compact["per_100_requests"]), 100 / 41)
            self.assertAlmostEqual(cast(float, sonnet_compact["per_active_hour"]), 1.5)
            self.assertAlmostEqual(cast(float, sonnet_compact["seconds_per_active_hour"]), 1.5)
            self.assertEqual(sonnet_compact["post_requests"], 10)
            self.assertEqual(sonnet_compact["other_requests"], 31)
            self.assertEqual(sonnet_compact["post_seconds_ratio"], 2)
            self.assertEqual(sonnet_compact["post_write_ratio"], 2)

            self.assertLess(report.index("| switched |"), report.index("| Pooled switched |"))
            self.assertLess(report.index("| Pooled switched |"), report.index("| sonnet-only |"))
            self.assertLess(report.index("## Concurrent control"), report.index("## Compactions"))
            self.assertEqual(report.count("Drops — "), 4)
            self.assertNotIn("SECRET_REQUEST_ID", report + output)

    def test_control_with_same_speedup_erases_raw_time_gain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            turns, compactions = fixture_rows()
            turns = [replace(row, seconds=5) if row.name == "control" and
                     row.started >= instant(420) else row for row in turns]
            write_fixture(state_dir, turns, compactions)
            result, _, _ = run_compare(state_dir)
            switched = named(records(result, "directors"), "switched")
            differences = record(switched, "differences")
            self.assertEqual(record(differences, "seconds")["label"], "faster")
            self.assertEqual(record(differences, "net_seconds")["value"], 0)
            self.assertEqual(record(differences, "net_seconds")["label"],
                             "no measurable difference")
            pooled = record(result, "pooled")
            self.assertEqual(record(record(pooled, "differences"), "net_seconds")["label"],
                             "no measurable difference")
            self.assertEqual(record(record(result, "control"), "pooled")["change_seconds"], -20)

    def test_missing_qualifying_control_marks_net_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            turns, compactions = fixture_rows()
            turns = [row for row in turns if row.name != "control" or row.started < instant(420)]
            write_fixture(state_dir, turns, compactions)
            result, _, _ = run_compare(state_dir)
            control = record(result, "control")
            self.assertEqual(records(control, "directors"), [])
            self.assertEqual(record(control, "pooled"), {
                "before_n": 0, "after_n": 0, "before_median": None,
                "after_median": None, "change_seconds": None,
            })
            switched = named(records(result, "directors"), "switched")
            net = record(record(switched, "differences"), "net_seconds")
            self.assertEqual(net["label"], "no control")
            self.assertIsNone(net["value"])

    def test_filtered_sonnet_director_never_joins_clock_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            turns, compactions = fixture_rows()
            write_fixture(state_dir, turns, compactions)
            without_director, _, _ = run_compare(state_dir)
            baseline_net = record(record(named(records(without_director, "directors"),
                                               "switched"), "differences"), "net_seconds")

            turns.extend(
                turn("filtered-switch", OPUS, minute, seconds=45)
                for minute in range(30)
            )
            turns.extend(
                turn("filtered-switch", OPUS, 420 + minute, seconds=25)
                for minute in range(30)
            )
            turns.append(turn("filtered-switch", SONNET, 400, seconds=10, effort="high"))
            write_fixture(state_dir, turns, compactions)
            result, report, _ = run_compare(state_dir)
            control = record(result, "control")
            filtered = named(records(result, "directors"), "filtered-switch")
            self.assertEqual(filtered["status"], "control candidate")
            self.assertEqual(record(record(filtered, "drops"), "sonnet")["effort"], 1)
            self.assertEqual(control["at_pdt"], record(without_director, "control")["at_pdt"])
            self.assertEqual([row["name"] for row in records(control, "directors")], ["control"])
            self.assertEqual(record(control, "pooled"), record(record(without_director, "control"), "pooled"))
            switched = named(records(result, "directors"), "switched")
            net = record(record(switched, "differences"), "net_seconds")
            self.assertEqual(net, baseline_net)
            control_report = report.split("## Concurrent control", 1)[1].split("## Compactions", 1)[0]
            self.assertNotIn("| filtered-switch |", control_report)

    def test_stale_extract_does_not_hide_ineligible_director_or_drops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            write_fixture(state_dir)
            result, report, _ = run_compare(state_dir)
            directors = records(result, "directors")
            self.assertEqual([row["name"] for row in directors],
                             ["switched", "sonnet-only", "ineligible", "control"])
            ineligible = named(directors, "ineligible")
            self.assertEqual(ineligible["status"], "no eligible requests")
            self.assertEqual(record(ineligible, "counts"), {
                "opus_all": 0, "opus_baseline": 0, "sonnet": 0, "other": 1,
            })
            self.assertEqual(record(ineligible, "drops"), {
                "opus": {"switch_turn": 1, "effort": 0, "speed": 0},
                "sonnet": {"switch_turn": 1, "effort": 0, "speed": 0},
                "other": {"switch_turn": 0, "effort": 0, "speed": 0},
            })
            self.assertIsNone(ineligible["differences"])
            self.assertEqual(ineligible["classes"], {})
            self.assertIn("| ineligible | 0 / 0 | 0 | 1 |", report)
            self.assertNotIn("| ineligible | opus", report)
            self.assertNotIn("| ineligible | sonnet |", report)
            for line in report.splitlines():
                if line.startswith("Drops — "):
                    self.assertIn("ineligible opus switch:1", line)
                    self.assertIn("ineligible sonnet switch:1", line)
                    self.assertIn("ineligible other switch:0", line)

    def test_control_requires_thirty_timed_continuations_on_each_side(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            turns, compactions = fixture_rows()
            turns = [row for row in turns if row.name != "control"]
            turns.extend(
                turn("untimed-after", OPUS, minute, seconds=12)
                for minute in range(30)
            )
            turns.extend(turn("untimed-after", OPUS, 420 + minute, seconds=None)
                         for minute in range(30))
            write_fixture(state_dir, turns, compactions)
            result, report, _ = run_compare(state_dir)
            control = record(result, "control")
            self.assertEqual(records(control, "directors"), [])
            self.assertEqual(record(control, "pooled")["before_n"], 0)
            self.assertEqual(record(control, "pooled")["after_n"], 0)
            switched = named(records(result, "directors"), "switched")
            self.assertEqual(record(record(switched, "differences"), "net_seconds")["label"],
                             "no control")
            control_report = report.split("## Concurrent control", 1)[1].split("## Compactions", 1)[0]
            self.assertNotIn("| untimed-after |", control_report)

            turns.extend(turn("timed-control", OPUS, minute, seconds=25)
                         for minute in range(30))
            turns.extend(turn("timed-control", OPUS, 420 + minute, seconds=20)
                         for minute in range(30))
            write_fixture(state_dir, turns, compactions)
            result, report, _ = run_compare(state_dir)
            control = record(result, "control")
            timed = named(records(control, "directors"), "timed-control")
            self.assertEqual((timed["before_n"], timed["after_n"]), (30, 30))
            self.assertEqual((record(control, "pooled")["before_n"],
                              record(control, "pooled")["after_n"]), (30, 30))
            self.assertEqual(record(record(named(records(result, "directors"),
                                                 "switched"), "differences"),
                                    "net_seconds")["label"], "faster")
            control_report = report.split("## Concurrent control", 1)[1].split("## Compactions", 1)[0]
            self.assertIn("| timed-control | — | 30 / 30 | 25.00 / 20.00 |", control_report)
            self.assertNotIn("| untimed-after |", control_report)

    def test_sonnet_only_class_table_has_only_sonnet_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            write_fixture(state_dir)
            _, report, _ = run_compare(state_dir)
            class_table = report.split("## By turn class", 1)[1].split("## Concurrent control", 1)[0]
            self.assertNotIn("| sonnet-only | opus_all |", class_table)
            self.assertNotIn("| sonnet-only | opus |", class_table)
            self.assertEqual(class_table.count("| sonnet-only | sonnet |"), 4)
            self.assertEqual(class_table.count("| switched | opus_all |"), 4)
            self.assertEqual(class_table.count("| switched | opus |"), 4)
            self.assertEqual(class_table.count("| Pooled switched | opus |"), 4)

    def test_unknown_compaction_duration_has_unknown_rate(self) -> None:
        for durations in ((None, None), (1000, None)):
            with self.subTest(durations=durations), tempfile.TemporaryDirectory() as temporary:
                state_dir = Path(temporary)
                turns, compactions = fixture_rows()
                compactions = [
                    compactions[0],
                    replace(compactions[1], duration_ms=durations[0]),
                    replace(compactions[1], at=instant(361, 30), duration_ms=durations[1]),
                ]
                write_fixture(state_dir, turns, compactions)
                result, report, _ = run_compare(state_dir)
                rows = records(result, "compactions")
                sonnet = next(row for row in rows if row["name"] == "switched" and row["arm"] == "sonnet")
                self.assertIsNone(sonnet["seconds_per_active_hour"])
                self.assertEqual(sonnet["duration_ms_median"], durations[0])
                sonnet_line = next(line for line in report.splitlines()
                                   if line.startswith("| switched | sonnet | 2 / 41 |"))
                self.assertEqual(sonnet_line.split("|")[8].strip(), "—")
                control = next(row for row in rows if row["name"] == "control" and row["arm"] == "opus")
                self.assertEqual(control["compactions"], 0)
                self.assertEqual(control["seconds_per_active_hour"], 0)
                control_line = next(line for line in report.splitlines()
                                    if line.startswith("| control | opus | 0 / 60 |"))
                self.assertEqual(control_line.split("|")[8].strip(), "0.00")

    def test_control_before_side_uses_latest_three_hundred_opus_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            turns, compactions = fixture_rows()
            turns.extend(
                turn("window-control", OPUS, minute,
                     seconds=1000 if minute < 60 else 20 if minute < 210 else 40,
                     kind="human" if minute >= 340 else "continuation")
                for minute in range(-40, 360)
            )
            turns.extend(turn("window-control", OPUS, 420 + minute, seconds=20)
                         for minute in range(30))
            write_fixture(state_dir, turns, compactions)
            result, _, _ = run_compare(state_dir)
            control = named(records(record(result, "control"), "directors"), "window-control")
            self.assertEqual((control["before_n"], control["after_n"]), (280, 30))
            self.assertEqual(control["before_median"], 20)
            self.assertEqual(control["after_median"], 20)
