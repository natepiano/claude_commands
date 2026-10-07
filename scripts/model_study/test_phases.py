"""Synthetic run-history tests for phase-level director measurements."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from stats import request_cost
from turns import Turn


SCRIPT = Path(__file__).with_name("model_study.py")
START = datetime(2026, 10, 6, tzinfo=timezone.utc)
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
type JsonRecord = dict[str, object]
type RunEvent = dict[str, object]


def instant(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


def turn(
    session: str, name: str, model: str, minute: int, seconds: float | None,
    *, switch_turn: bool = False, effort: str = "xhigh", speed: str = "standard",
) -> Turn:
    return Turn(
        session=session, name=name, request_id=f"SECRET_REQUEST_{minute}",
        started=instant(minute), ended=instant(minute), seconds=seconds,
        model=model, effort=effort, speed=speed, stop="end_turn",
        kind="continuation", input=1000, output=100, thinking=25,
        cache_read=200, write_5m=10, write_1h=0, context=1210,
        switch_turn=switch_turn, after_compact=0,
    )


def event(event_type: str, minute: int, phase_id: str = "") -> RunEvent:
    row: RunEvent = {"event_type": event_type, "timestamp": instant(minute)}
    if phase_id:
        row["phase_instance_id"] = phase_id
    return row


def phase_start(
    minute: int, phase_id: str, title: str, words: int, lines: int,
) -> RunEvent:
    row = event("phase_started", minute, phase_id)
    row.update({
        "phase_id": phase_id, "phase_title": title,
        "plan_doc": "plans/synthetic-plan.md",
        "work_order_words": words, "work_order_lines": lines,
    })
    return row


def phase_finish(minute: int, phase_id: str, status: str = "completed") -> RunEvent:
    row = event("phase_finished", minute, phase_id)
    row["status"] = status
    return row


def write_jsonl(path: Path, rows: list[RunEvent] | list[Turn]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(
        "".join(json.dumps(row.to_json() if isinstance(row, Turn) else row) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_fixture(root: Path) -> tuple[Path, Path, Path, list[Turn]]:
    state_dir = root / "state"
    runs_dir = root / "runs"
    registry_dir = root / "registry"
    state_dir.mkdir()
    runs_dir.mkdir()
    registry_dir.mkdir()
    roster = root / "roster.json"
    _ = roster.write_text(json.dumps([
        {"name": "switched", "session_ids": ["switch-session"],
         "switched_by_pdt": "2026-10-06T16:00", "production": "synthetic",
         "director_from_pdt": None},
        {"name": "control", "session_ids": ["control-session"],
         "switched_by_pdt": None, "production": "synthetic",
         "director_from_pdt": None},
    ]), encoding="utf-8")
    turns = [
        turn("switch-session", "switched", OPUS, 1, 10, switch_turn=True, effort="high"),
        turn("switch-session", "switched", OPUS, 2, None, speed="fast"),
        turn("switch-session", "switched", SONNET, 21, 4, switch_turn=True),
        turn("switch-session", "switched", OPUS, 41, 8),
        turn("switch-session", "switched", SONNET, 42, 6),
        turn("switch-session", "switched", OPUS, 61, 7),
        turn("switch-session", "switched", SONNET, 101, 3),
        turn("control-session", "control", OPUS, 121, 20),
        turn("control-session", "control", SONNET, 141, 5),
    ]
    write_jsonl(state_dir / "turns.jsonl", turns)
    switched_run = [
        {**event("run_started", 0), "main_agent": {"session_id": "switch-session"}},
        phase_start(0, "opus-phase", "Opus work", 500, 20),
        event("finding_opened", 3, "opus-phase"),
        event("finding_opened", 4, "opus-phase"),
        {**event("finding_batch_dispatched", 5, "opus-phase"), "round": 1},
        {**event("finding_batch_dispatched", 6, "opus-phase"), "round": 3},
        event("finding_batch_abandoned", 7, "opus-phase"),
        phase_finish(10, "opus-phase"),
        phase_start(20, "sonnet-phase", "Sonnet work", 1000, 30),
        phase_finish(30, "sonnet-phase"),
        phase_start(40, "mixed-phase", "Mixed work", 200, 5),
        phase_finish(50, "mixed-phase"),
        phase_start(60, "stopped-phase", "Stopped work", 200, 5),
        phase_finish(70, "stopped-phase", "stopped"),
        phase_start(80, "empty-phase", "Empty work", 200, 5),
        phase_finish(90, "empty-phase"),
        phase_start(100, "final", "Zero words", 0, 0),
        phase_finish(110, "final"),
    ]
    write_jsonl(runs_dir / "switched.jsonl", switched_run)
    write_jsonl(runs_dir / "control.jsonl", [
        {**event("run_started", 120), "main_agent": {"session_id": "control-session"}},
        phase_start(120, "control-opus", "Control Opus", 1000, 10),
        phase_finish(130, "control-opus"),
        phase_start(140, "control-sonnet", "Control Sonnet", 1000, 10),
        phase_finish(150, "control-sonnet"),
    ])
    write_jsonl(runs_dir / "unrostered.jsonl", [
        {**event("run_started", 0), "main_agent": {"session_id": "not-in-roster"}},
        phase_start(0, "unrostered-phase", "Unrostered work", 100, 5),
        phase_finish(10, "unrostered-phase"),
    ])
    return state_dir, runs_dir, roster, turns


def phase_rows(result: JsonRecord) -> list[JsonRecord]:
    value = result["phases"]
    assert isinstance(value, list)
    return cast(list[JsonRecord], value)


def named_phase(rows: list[JsonRecord], phase_id: str) -> JsonRecord:
    return next(row for row in rows if row["phase_id"] == phase_id)


def run_phases(state_dir: Path, runs_dir: Path, roster: Path) -> tuple[JsonRecord, str, str]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "phases", "--state-dir", str(state_dir),
         "--runs-dir", str(runs_dir), "--roster", str(roster),
         "--registry-dir", str(state_dir.parent / "registry")],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    output = (state_dir / "phases.json").read_text(encoding="utf-8")
    return cast(JsonRecord, json.loads(output)), completed.stdout, output


class PhaseTests(unittest.TestCase):
    def test_join_counts_completed_phase_requests_and_repair_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir, runs_dir, roster, turns = write_fixture(Path(temporary))
            result, report, output = run_phases(state_dir, runs_dir, roster)
            rows = phase_rows(result)
            self.assertEqual(len(rows), 6)
            self.assertEqual({cast(str, row["phase_id"]) for row in rows}, {
                "opus-phase", "sonnet-phase", "mixed-phase", "final",
                "control-opus", "control-sonnet",
            })
            opus = named_phase(rows, "opus-phase")
            self.assertEqual(opus["director"], "switched")
            self.assertEqual(opus["plan"], "synthetic-plan")
            self.assertEqual(opus["phase_title"], "Opus work")
            self.assertEqual(opus["arm"], "opus")
            self.assertEqual(opus["requests"], 2)
            self.assertEqual(opus["director_seconds"], 10)
            self.assertEqual(opus["output_tokens"], 200)
            self.assertAlmostEqual(cast(float, opus["cost_usd"]), sum(request_cost(turn) for turn in turns[:2]))
            self.assertEqual(opus["repair_rounds"], 3)
            self.assertEqual(opus["findings_opened"], 2)
            self.assertEqual(opus["abandoned_batches"], 1)
            self.assertEqual(opus["phase_elapsed_seconds"], 600)
            self.assertEqual(opus["work_order_words"], 500)
            self.assertEqual(opus["work_order_lines"], 20)
            self.assertEqual(opus["requests_per_1000_words"], 4)
            self.assertEqual(opus["seconds_per_1000_words"], 20)
            self.assertAlmostEqual(
                cast(float, opus["cost_per_1000_words"]),
                2 * sum(request_cost(turn) for turn in turns[:2]),
            )
            self.assertEqual(named_phase(rows, "mixed-phase")["arm"], "mixed")
            sonnet = named_phase(rows, "sonnet-phase")
            self.assertEqual(sonnet["arm"], "sonnet")
            self.assertEqual(sonnet["requests"], 1)
            self.assertEqual(sonnet["seconds_per_1000_words"], 4)
            self.assertIsNone(named_phase(rows, "final")["requests_per_1000_words"])
            self.assertIsNone(named_phase(rows, "final")["seconds_per_1000_words"])
            self.assertIsNone(named_phase(rows, "final")["cost_per_1000_words"])
            drops = cast(JsonRecord, result["drops"])
            self.assertEqual(drops, {"stopped": 1, "errored": 0, "empty": 1})
            self.assertIn("Opus work", report)
            self.assertIn("Mixed work", report)
            self.assertIn("Zero words", report)
            self.assertNotIn("SECRET_REQUEST", report + output)
            self.assertNotIn("Unrostered work", report + output)

    def test_short_phase_samples_are_listed_with_too_few_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir, runs_dir, roster, _ = write_fixture(Path(temporary))
            result, report, _ = run_phases(state_dir, runs_dir, roster)
            comparisons = cast(list[JsonRecord], result["comparisons"])
            self.assertEqual([row["director"] for row in comparisons], ["switched", "Pooled switched"])
            switched_metrics = cast(JsonRecord, comparisons[0]["metrics"])
            rates = cast(JsonRecord, switched_metrics["requests_per_1000_words"])
            self.assertEqual((rates["opus_n"], rates["sonnet_n"]), (1, 1))
            self.assertEqual(rates["label"], "too few phases (n=1 against 1)")
            self.assertIsNone(rates["low"])
            self.assertIsNone(rates["high"])
            repairs = cast(JsonRecord, switched_metrics["repair_rounds"])
            self.assertEqual((repairs["opus_n"], repairs["sonnet_n"]), (1, 2))
            self.assertEqual(repairs["label"], "too few phases (n=1 against 2)")
            self.assertIn("too few phases (n=1 against 2)", report)
            self.assertIn("Opus work", report)
            self.assertIn("Sonnet work", report)
            self.assertIn("Zero words", report)
            self.assertNotIn("Stopped work", report)
            self.assertNotIn("Empty work", report)
            self.assertNotIn("Unrostered work", report)
            self.assertEqual(len(phase_rows(result)), 6)

    def test_registry_session_joins_restarted_director_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir, runs_dir, roster, _ = write_fixture(Path(temporary))
            registry = state_dir.parent / "registry"
            _ = (registry / "12345.json").write_text(
                json.dumps({"name": "switched", "sessionId": "restart-session"}), encoding="utf-8",
            )
            write_jsonl(runs_dir / "restart.jsonl", [
                {**event("run_started", 160), "main_agent": {"session_id": "restart-session"}},
                phase_start(160, "restarted", "Restarted work", 1000, 10),
                phase_finish(170, "restarted"),
            ])
            with (state_dir / "turns.jsonl").open("a", encoding="utf-8") as output:
                _ = output.write(json.dumps(turn("restart-session", "switched", SONNET, 161, 6).to_json()) + "\n")
            result, _, _ = run_phases(state_dir, runs_dir, roster)
            restarted = named_phase(phase_rows(result), "restarted")
            self.assertEqual(restarted["director"], "switched")
            self.assertEqual(restarted["requests"], 1)

    def test_roster_rename_joins_requests_by_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir, runs_dir, roster, _ = write_fixture(Path(temporary))
            entries = cast(list[JsonRecord], json.loads(roster.read_text(encoding="utf-8")))
            entries[0]["name"] = "renamed"
            _ = roster.write_text(json.dumps(entries), encoding="utf-8")
            result, _, _ = run_phases(state_dir, runs_dir, roster)
            opus = named_phase(phase_rows(result), "opus-phase")
            self.assertEqual(opus["director"], "renamed")
            self.assertEqual(opus["requests"], 2)
            self.assertEqual(opus["arm"], "opus")
            self.assertEqual(cast(JsonRecord, result["drops"])["empty"], 1)

    def test_missing_invalid_and_zero_work_order_sizes_keep_distinct_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir, runs_dir, roster, _ = write_fixture(Path(temporary))
            missing = phase_start(160, "missing-size", "Missing size", 1000, 10)
            del missing["work_order_words"]
            del missing["work_order_lines"]
            negative = phase_start(180, "negative-size", "Negative size", -1, -2)
            noninteger = phase_start(200, "noninteger-size", "Noninteger size", 1000, 10)
            noninteger["work_order_words"] = "unknown"
            noninteger["work_order_lines"] = True
            with (runs_dir / "switched.jsonl").open("a", encoding="utf-8") as output:
                for row in (
                    missing, phase_finish(170, "missing-size"),
                    negative, phase_finish(190, "negative-size"),
                    noninteger, phase_finish(210, "noninteger-size"),
                ):
                    _ = output.write(json.dumps(row) + "\n")
            with (state_dir / "turns.jsonl").open("a", encoding="utf-8") as output:
                for minute in (161, 181, 201):
                    _ = output.write(json.dumps(turn("switch-session", "switched", SONNET, minute, 5).to_json()) + "\n")
            result, report, _ = run_phases(state_dir, runs_dir, roster)
            rows = phase_rows(result)
            for phase_id in ("missing-size", "negative-size", "noninteger-size"):
                row = named_phase(rows, phase_id)
                self.assertIsNone(row["work_order_words"])
                self.assertIsNone(row["work_order_lines"])
                for metric in ("requests_per_1000_words", "seconds_per_1000_words", "cost_per_1000_words"):
                    self.assertIsNone(row[metric])
            zero = named_phase(rows, "final")
            self.assertEqual((zero["work_order_words"], zero["work_order_lines"]), (0, 0))
            self.assertIsNone(zero["requests_per_1000_words"])
            self.assertIsNone(zero["seconds_per_1000_words"])
            self.assertIsNone(zero["cost_per_1000_words"])
            self.assertIn("Missing size | sonnet", report)
            self.assertIn("— / —", report)
            self.assertIn("0 / 0", report)

    def test_error_finish_is_counted_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir, runs_dir, roster, _ = write_fixture(Path(temporary))
            with (runs_dir / "switched.jsonl").open("a", encoding="utf-8") as output:
                for row in (phase_start(160, "error-phase", "Errored work", 100, 5), phase_finish(170, "error-phase", "error")):
                    _ = output.write(json.dumps(row) + "\n")
            result, report, _ = run_phases(state_dir, runs_dir, roster)
            self.assertEqual(result["drops"], {"stopped": 1, "errored": 1, "empty": 1})
            self.assertIn("Dropped phases: stopped 1; errored 1; no requests 1.", report)
            self.assertNotIn("Errored work", report)

    def test_bootstrap_labels_cover_more_fewer_and_overlapping_phases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir, runs_dir, roster, original_turns = write_fixture(Path(temporary))
            events: list[RunEvent] = [
                {**event("run_started", 200), "main_agent": {"session_id": "switch-session"}},
            ]
            measured_turns = [row for row in original_turns if row.session == "control-session"]
            for index in range(16):
                minute = 200 + index * 3
                phase_id = f"sample-{index}"
                is_sonnet = index >= 8
                events.append(phase_start(minute, phase_id, phase_id, 500 if is_sonnet else 1000, 10))
                if index % 8 < 4:
                    events.append({**event("finding_batch_dispatched", minute + 1, phase_id), "round": 1})
                events.append(phase_finish(minute + 2, phase_id))
                measured_turns.append(turn(
                    "switch-session", "switched", SONNET if is_sonnet else OPUS,
                    minute + 1, 5 if is_sonnet else 20,
                ))
            write_jsonl(runs_dir / "switched.jsonl", events)
            write_jsonl(state_dir / "turns.jsonl", measured_turns)
            result, _, _ = run_phases(state_dir, runs_dir, roster)
            comparisons = cast(list[JsonRecord], result["comparisons"])
            metrics = cast(JsonRecord, comparisons[0]["metrics"])
            for metric, label in (
                ("requests_per_1000_words", "more"),
                ("seconds_per_1000_words", "fewer"),
                ("repair_rounds", "no measurable difference"),
            ):
                difference = cast(JsonRecord, metrics[metric])
                self.assertEqual((difference["opus_n"], difference["sonnet_n"]), (8, 8))
                self.assertEqual(difference["label"], label)
                self.assertIsNotNone(difference["low"])
                self.assertIsNotNone(difference["high"])

    def test_pooled_comparison_excludes_unswitched_director(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir, runs_dir, roster, _ = write_fixture(Path(temporary))
            result, _, _ = run_phases(state_dir, runs_dir, roster)
            comparisons = cast(list[JsonRecord], result["comparisons"])
            self.assertEqual([row["director"] for row in comparisons], ["switched", "Pooled switched"])
            switched = cast(JsonRecord, comparisons[0]["metrics"])
            pooled = cast(JsonRecord, comparisons[1]["metrics"])
            for metric in ("requests_per_1000_words", "seconds_per_1000_words", "cost_per_1000_words", "repair_rounds"):
                switched_result = cast(JsonRecord, switched[metric])
                pooled_result = cast(JsonRecord, pooled[metric])
                self.assertEqual(
                    (pooled_result["opus_n"], pooled_result["sonnet_n"]),
                    (switched_result["opus_n"], switched_result["sonnet_n"]),
                )
            self.assertEqual((cast(JsonRecord, pooled["repair_rounds"])["opus_n"], cast(JsonRecord, pooled["repair_rounds"])["sonnet_n"]), (1, 2))

    def test_phase_rows_keep_run_event_order_with_numeric_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir, runs_dir, roster, original_turns = write_fixture(Path(temporary))
            write_jsonl(runs_dir / "switched.jsonl", [
                {**event("run_started", 200), "main_agent": {"session_id": "switch-session"}},
                phase_start(200, "10", "Tenth phase", 1000, 10),
                phase_finish(205, "10"),
                phase_start(210, "2", "Second phase", 1000, 10),
                phase_finish(215, "2"),
            ])
            write_jsonl(state_dir / "turns.jsonl", [
                *[row for row in original_turns if row.session == "control-session"],
                turn("switch-session", "switched", OPUS, 201, 5),
                turn("switch-session", "switched", SONNET, 211, 5),
            ])
            result, _, _ = run_phases(state_dir, runs_dir, roster)
            self.assertEqual(
                [row["phase_id"] for row in phase_rows(result) if row["director"] == "switched"],
                ["10", "2"],
            )


if __name__ == "__main__":
    _ = unittest.main()
