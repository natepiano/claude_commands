"""Synthetic regression tests for rerunning the director model study."""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

import model_study
import report as study_report
from compare import ComparisonReport, compare
from phases import PhaseReport, phases
from test_report import comparison_fixture, director, extract_fixture, phase_fixture
from test_turns import TranscriptRecord, assistant, user, write_transcript


FIRST = "2026-10-06T19:00:00-07:00"
SECOND = "2026-10-06T20:00:00-07:00"
SECRET_PROMPT = "SECRET_RERUN_PROMPT"
SECRET_ARGUMENT = "SECRET_RERUN_TOOL_ARGUMENT"
SECRET_PATH = "/private/SECRET_RERUN_PATH"
type JsonRecord = dict[str, object]


def run_cli(*arguments: str) -> tuple[int, str]:
    output = io.StringIO()
    with patch.object(sys, "argv", ["model_study.py", *arguments]), redirect_stdout(output):
        try:
            model_study.main()
        except SystemExit as stopped:
            return cast(int, stopped.code), output.getvalue()
    return 0, output.getvalue()


def read_json(path: Path) -> JsonRecord:
    return cast(JsonRecord, json.loads(path.read_text(encoding="utf-8")))


def read_lines(path: Path) -> list[JsonRecord]:
    return [cast(JsonRecord, json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]


def write_roster(path: Path, session_ids: list[str]) -> None:
    _ = path.write_text(json.dumps([{
        "name": "alpha", "session_ids": session_ids,
        "switched_by_pdt": "2026-10-06T15:00",
        "production": "synthetic", "director_from_pdt": None,
    }]), encoding="utf-8")


def transcript(
    at: str, request_id: str, *, model: str = "claude-opus-5-5",
    sidechain: bool = False, compact: bool = False,
) -> list[TranscriptRecord]:
    minute = at[:16]
    records = [
        user(f"{minute}:00Z", SECRET_PROMPT, origin="human"),
        assistant(f"{minute}:01Z", request_id, model=model, effort="xhigh",
                  usage={"input_tokens": 50, "output_tokens": 20, "speed": "standard"}),
    ]
    if sidechain:
        records.append(assistant(f"{minute}:02Z", f"side-{request_id}", sidechain=True))
    records.extend([
        user(f"{minute}:03Z", [{"type": "tool_result", "content": SECRET_ARGUMENT}]),
        {"type": "attachment", "timestamp": f"{minute}:04Z",
         "message": {"role": "user", "content": SECRET_PATH}},
    ])
    if compact:
        records.append({
            "type": "system", "subtype": "compact_boundary", "timestamp": f"{minute}:05Z",
            "compactMetadata": {"trigger": "manual", "preTokens": 1000,
                                "postTokens": 200, "durationMs": 50},
        })
    return records


def extract_cli(root: Path, at: str = FIRST) -> tuple[int, str]:
    return run_cli(
        "extract", "--state-dir", str(root / "state"),
        "--projects-dir", str(root / "projects"),
        "--registry-dir", str(root / "registry"),
        "--roster", str(root / "roster.json"), "--now", at,
    )


def report_cli(root: Path, at: str, *flags: str) -> tuple[int, str]:
    return run_cli(
        "report", "--state-dir", str(root / "state"),
        "--no-extract", "--now", at, *flags,
    )


def results_cli(root: Path, docs: Path, at: str, *flags: str) -> tuple[int, str]:
    return run_cli(
        "results", "--state-dir", str(root / "state"),
        "--docs-dir", str(docs), "--no-extract", "--now", at, *flags,
    )


def prepare_report(root: Path) -> None:
    state = root / "state"
    state.mkdir(parents=True)
    _ = (state / "extract.json").write_text(json.dumps(extract_fixture()), encoding="utf-8")


def comparison_writer(data: ComparisonReport) -> Callable[[Path], ComparisonReport]:
    def write(state_dir: Path) -> ComparisonReport:
        _ = (state_dir / "compare.json").write_text(json.dumps(data), encoding="utf-8")
        return data
    return write


def phases_writer(data: PhaseReport) -> Callable[..., PhaseReport]:
    def write(state_dir: Path, *_args: object) -> PhaseReport:
        _ = (state_dir / "phases.json").write_text(json.dumps(data), encoding="utf-8")
        return data
    return write


class CarryForwardTests(unittest.TestCase):
    def test_extract_ignores_subagent_transcripts_beside_director_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            (root / "registry").mkdir()
            write_roster(root / "roster.json", ["director-id"])
            _ = write_transcript(
                project, transcript("2026-10-06T23:00", "director-request"), "director-id"
            )
            subagents = project / "director-id" / "subagents"
            subagents.mkdir(parents=True)
            _ = write_transcript(
                subagents, transcript("2026-10-06T23:01", "subagent-request"), "agent-1"
            )

            status, _ = run_cli(
                "extract", "--state-dir", str(root / "state"),
                "--projects-dir", str(root / "projects"),
                "--registry-dir", str(root / "registry"),
                "--roster", str(root / "roster.json"), "--now", FIRST,
            )
            self.assertEqual(status, 0)
            self.assertEqual(
                [row["request_id"] for row in read_lines(root / "state" / "turns.jsonl")],
                ["director-request"],
            )
            states = cast(dict[str, JsonRecord], read_json(root / "state" / "extract.json")["session_states"])
            self.assertEqual(set(states), {"director-id"})

    def test_legacy_extract_keeps_registry_session_after_registration_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            registry = root / "registry"
            registry.mkdir()
            write_roster(root / "roster.json", ["roster-id"])
            _ = write_transcript(project, transcript("2026-10-06T23:01", "roster"), "roster-id")
            added = write_transcript(project, [
                user("2026-10-06T22:59:59Z", "<command-name>/unit:delegate</command-name>"),
                *transcript("2026-10-06T23:00", "added", sidechain=True),
            ], "registry-id")
            registration = registry / "director.json"
            _ = registration.write_text(
                json.dumps({"name": "alpha", "sessionId": "registry-id"}), encoding="utf-8"
            )
            self.assertEqual(extract_cli(root)[0], 0)
            state = root / "state"
            legacy = read_json(state / "extract.json")
            old_summary = cast(list[JsonRecord], legacy["sessions"])[0]
            _ = legacy.pop("session_states")
            _ = (state / "extract.json").write_text(json.dumps(legacy), encoding="utf-8")
            registration.unlink()
            added.unlink()

            self.assertEqual(extract_cli(root)[0], 0)
            turns = read_lines(state / "turns.jsonl")
            self.assertEqual({row["session"] for row in turns}, {"roster-id", "registry-id"})
            extracted = read_json(state / "extract.json")
            summary = cast(list[JsonRecord], extracted["sessions"])[0]
            states = cast(dict[str, JsonRecord], extracted["session_states"])
            self.assertEqual(set(states), {"roster-id", "registry-id"})
            for state_row in states.values():
                self.assertEqual(set(state_row), {
                    "director", "requests", "dropped", "transcript_gone",
                    "director_from", "whole_session",
                })
            self.assertEqual(states["registry-id"]["transcript_gone"], True)
            self.assertEqual(states["registry-id"]["requests"], 1)
            self.assertEqual(states["registry-id"]["director_from"], old_summary["director_from"])
            self.assertEqual(states["registry-id"]["whole_session"], old_summary["whole_session"])
            self.assertEqual(states["roster-id"]["requests"], 1)
            self.assertEqual(summary["requests"], 2)
            self.assertEqual(summary["missing_ids"], ["registry-id"])
            self.assertEqual(summary["dropped"], old_summary["dropped"])
            outputs = ("turns.jsonl", "compactions.jsonl", "extract.json")
            first = {name: (state / name).read_bytes() for name in outputs}
            self.assertEqual(extract_cli(root)[0], 0)
            self.assertEqual({name: (state / name).read_bytes() for name in outputs}, first)

    def test_legacy_extract_assigns_unallocated_drops_to_gone_roster_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            (root / "registry").mkdir()
            write_roster(root / "roster.json", ["gone", "live"])
            gone = write_transcript(
                project, transcript("2026-10-06T23:00", "gone", sidechain=True), "gone"
            )
            _ = write_transcript(
                project, transcript("2026-10-06T23:02", "live-old", sidechain=True), "live"
            )
            self.assertEqual(extract_cli(root)[0], 0)
            state = root / "state"
            legacy = read_json(state / "extract.json")
            old_drops = cast(JsonRecord, cast(list[JsonRecord], legacy["sessions"])[0]["dropped"])
            _ = legacy.pop("session_states")
            _ = (state / "extract.json").write_text(json.dumps(legacy), encoding="utf-8")
            gone.unlink()
            _ = write_transcript(
                project, transcript("2026-10-06T23:02", "live-new", sidechain=True), "live"
            )

            self.assertEqual(extract_cli(root)[0], 0)
            extracted = read_json(state / "extract.json")
            summary = cast(list[JsonRecord], extracted["sessions"])[0]
            states = cast(dict[str, JsonRecord], extracted["session_states"])
            self.assertEqual({row["session"] for row in read_lines(state / "turns.jsonl")}, {"gone", "live"})
            self.assertEqual(summary["missing_ids"], ["gone"])
            self.assertEqual(summary["dropped"], old_drops)
            gone_drops = cast(JsonRecord, states["gone"]["dropped"])
            live_drops = cast(JsonRecord, states["live"]["dropped"])
            self.assertEqual(gone_drops["sidechain"], 1)
            self.assertEqual(live_drops["sidechain"], 1)
            self.assertEqual(
                {field: cast(int, gone_drops[field]) + cast(int, live_drops[field])
                 for field in old_drops}, old_drops,
            )
            self.assertEqual(states["gone"]["requests"], 1)
            self.assertEqual(states["gone"]["transcript_gone"], True)
            self.assertEqual(states["live"]["requests"], 1)
            self.assertEqual(states["live"]["transcript_gone"], False)

    def test_live_boundary_replaces_prior_whole_session_with_carried_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            (root / "registry").mkdir()
            write_roster(root / "roster.json", ["gone", "live"])
            boundary = "<command-name>/unit:delegate</command-name>"
            gone = write_transcript(project, [
                user("2026-10-06T22:59:59Z", boundary, origin="human"),
                *transcript("2026-10-06T23:00", "gone"),
            ], "gone")
            _ = write_transcript(project, transcript("2026-10-06T23:02", "live"), "live")
            self.assertEqual(extract_cli(root)[0], 0)
            state = root / "state"
            first = cast(list[JsonRecord], read_json(state / "extract.json")["sessions"])[0]
            self.assertEqual(first["whole_session"], True)
            gone.unlink()
            _ = write_transcript(project, [
                user("2026-10-06T23:01:59Z", boundary, origin="human"),
                *transcript("2026-10-06T23:02", "live"),
            ], "live")

            self.assertEqual(extract_cli(root)[0], 0)
            extracted = read_json(state / "extract.json")
            summary = cast(list[JsonRecord], extracted["sessions"])[0]
            states = cast(dict[str, JsonRecord], extracted["session_states"])
            self.assertEqual(summary["director_from"], "2026-10-06T22:59:59+00:00")
            self.assertEqual(summary["whole_session"], False)
            self.assertEqual(states["gone"]["director_from"], "2026-10-06T22:59:59+00:00")
            self.assertEqual(states["gone"]["whole_session"], False)
            self.assertEqual(states["live"]["director_from"], "2026-10-06T23:01:59+00:00")
            self.assertEqual(states["live"]["whole_session"], False)
            self.assertEqual(states["gone"]["transcript_gone"], True)
            _ = write_transcript(project, [
                user("2026-10-06T22:58:59Z", boundary, origin="human"),
                *transcript("2026-10-06T23:02", "live"),
            ], "live")
            self.assertEqual(extract_cli(root)[0], 0)
            moved = cast(list[JsonRecord], read_json(state / "extract.json")["sessions"])[0]
            self.assertEqual(moved["director_from"], "2026-10-06T22:58:59+00:00")

    def test_only_gone_session_keeps_rows_counts_and_missing_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            (root / "registry").mkdir()
            write_roster(root / "roster.json", ["gone"])
            gone = write_transcript(
                project, transcript("2026-10-06T23:00", "only", sidechain=True), "gone"
            )
            self.assertEqual(extract_cli(root)[0], 0)
            state = root / "state"
            first = read_json(state / "extract.json")
            old_drops = cast(list[JsonRecord], first["sessions"])[0]["dropped"]
            old_turns = (state / "turns.jsonl").read_bytes()
            gone.unlink()

            self.assertEqual(extract_cli(root)[0], 0)
            extracted = read_json(state / "extract.json")
            summary = cast(list[JsonRecord], extracted["sessions"])[0]
            states = cast(dict[str, JsonRecord], extracted["session_states"])
            self.assertEqual((state / "turns.jsonl").read_bytes(), old_turns)
            self.assertEqual(summary["requests"], 1)
            self.assertEqual(summary["dropped"], old_drops)
            self.assertEqual(summary["missing_ids"], ["gone"])
            self.assertEqual(states["gone"]["requests"], 1)
            self.assertEqual(states["gone"]["dropped"], old_drops)
            self.assertEqual(states["gone"]["transcript_gone"], True)

    def test_gone_and_live_sessions_keep_separate_counts_and_refresh_live_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            (root / "registry").mkdir()
            write_roster(root / "roster.json", ["gone", "live"])
            gone = write_transcript(project, transcript("2026-10-06T23:00", "old", sidechain=True, compact=True), "gone")
            live = write_transcript(project, transcript("2026-10-06T23:02", "live-old", sidechain=True), "live")
            self.assertEqual(extract_cli(root)[0], 0)
            first = read_json(root / "state" / "extract.json")
            self.assertEqual(first["extracted"], "2026-10-07T02:00:00+00:00")
            first_compactions = (root / "state" / "compactions.jsonl").read_bytes()
            self.assertEqual(len(first_compactions.splitlines()), 1)

            gone.unlink()
            _ = write_transcript(project, transcript("2026-10-06T23:02", "live-new"), "live")
            status, printed = extract_cli(root)
            self.assertEqual(status, 0)
            turns = read_lines(root / "state" / "turns.jsonl")
            self.assertEqual([row["request_id"] for row in turns], ["old", "live-new"])
            self.assertEqual([row["switch_turn"] for row in turns], [True, False])
            self.assertEqual((root / "state" / "compactions.jsonl").read_bytes(), first_compactions)
            summary = cast(list[JsonRecord], read_json(root / "state" / "extract.json")["sessions"])[0]
            self.assertEqual(summary["requests"], 2)
            self.assertEqual(summary["missing_ids"], ["gone"])
            drops = cast(JsonRecord, summary["dropped"])
            self.assertEqual(drops["sidechain"], 1)
            states = cast(dict[str, JsonRecord], read_json(root / "state" / "extract.json")["session_states"])
            self.assertEqual(len(states), 2)
            self.assertEqual(set(states), {"gone", "live"})
            self.assertEqual(states["gone"]["transcript_gone"], True)
            self.assertEqual(states["live"]["transcript_gone"], False)
            self.assertEqual([states[session]["requests"] for session in ("gone", "live")], [1, 1])
            self.assertEqual(cast(JsonRecord, states["gone"]["dropped"])["sidechain"], 1)
            self.assertEqual(cast(JsonRecord, states["live"]["dropped"])["sidechain"], 0)
            self.assertIn("carried", printed)
            self.assertNotIn("live-old", (root / "state" / "turns.jsonl").read_text())
            self.assertTrue(live.exists())
            outputs = ("turns.jsonl", "compactions.jsonl", "extract.json")
            current = {name: (root / "state" / name).read_bytes() for name in outputs}
            self.assertEqual(extract_cli(root)[0], 0)
            self.assertEqual({name: (root / "state" / name).read_bytes() for name in outputs}, current)
            comparison = compare(root / "state")
            compared = (root / "state" / "compare.json").read_bytes()
            self.assertEqual(compare(root / "state"), comparison)
            self.assertEqual((root / "state" / "compare.json").read_bytes(), compared)
            runs = root / "runs"
            runs.mkdir()
            phase_data = phases(root / "state", runs, root / "roster.json", root / "registry")
            measured = (root / "state" / "phases.json").read_bytes()
            self.assertEqual(phases(root / "state", runs, root / "roster.json", root / "registry"), phase_data)
            self.assertEqual((root / "state" / "phases.json").read_bytes(), measured)

    def test_registry_added_id_stays_joined_after_registry_and_transcript_disappear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            registry = root / "registry"
            registry.mkdir()
            write_roster(root / "roster.json", ["roster-id"])
            _ = write_transcript(project, [], "roster-id")
            added = write_transcript(project, transcript("2026-10-06T23:00", "added"), "registry-id")
            registration = registry / "director.json"
            _ = registration.write_text(json.dumps({"name": "alpha", "sessionId": "registry-id"}), encoding="utf-8")
            self.assertEqual(extract_cli(root)[0], 0)
            registration.unlink()
            added.unlink()
            self.assertEqual(extract_cli(root)[0], 0)
            states = cast(dict[str, JsonRecord], read_json(root / "state" / "extract.json")["session_states"])
            self.assertEqual(set(states), {"roster-id", "registry-id"})
            self.assertEqual(states["registry-id"]["transcript_gone"], True)
            self.assertEqual(states["registry-id"]["requests"], 1)

            runs = root / "runs"
            runs.mkdir()
            events: list[JsonRecord] = [
                {"event_type": "run_started", "timestamp": "2026-10-06T22:59:00Z",
                 "main_agent": {"session_id": "registry-id"}},
                {"event_type": "phase_started", "timestamp": "2026-10-06T23:00:00Z",
                 "phase_instance_id": "phase-one", "phase_id": "phase-one",
                 "phase_title": "Synthetic phase", "plan_doc": "plans/synthetic.md",
                 "work_order_words": 100, "work_order_lines": 5},
                {"event_type": "phase_finished", "timestamp": "2026-10-06T23:01:00Z",
                 "phase_instance_id": "phase-one", "status": "completed"},
            ]
            _ = (runs / "run.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
            result = phases(root / "state", runs, root / "roster.json", registry)
            self.assertEqual([(row["phase_id"], row["director"], row["requests"])
                              for row in result["phases"]], [("phase-one", "alpha", 1)])


class HistoryTests(unittest.TestCase):
    def test_render_and_message_without_history_omit_since_last_run(self) -> None:
        comparison = comparison_fixture()
        phase_data = phase_fixture()
        extracted = extract_fixture()
        document = study_report.render(
            comparison, phase_data, extracted, study_report.SAMPLE_DEADLINE_PDT
        )
        short = study_report.message(
            comparison, phase_data, extracted, study_report.SAMPLE_DEADLINE_PDT,
            False, Path("report.md"),
        )
        self.assertNotIn("Since the last run", document)
        self.assertNotIn("Pooled switched: first run.", short)
        self.assertNotIn("Pooled switched: median change then", short)

    def test_report_and_ready_clock_reaches_live_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            (root / "registry").mkdir()
            (root / "runs").mkdir()
            write_roster(root / "roster.json", ["one"])
            _ = write_transcript(project, transcript("2026-10-06T23:00", "one"), "one")
            flags = ("--state-dir", str(root / "state"), "--projects-dir", str(root / "projects"),
                     "--registry-dir", str(root / "registry"), "--roster", str(root / "roster.json"))
            report_flags = (*flags, "--runs-dir", str(root / "runs"), "--now", FIRST, "--interim")
            self.assertEqual(run_cli("report", *report_flags)[0], 0)
            state = root / "state"
            self.assertEqual(read_json(state / "extract.json")["extracted"], "2026-10-07T02:00:00+00:00")
            self.assertTrue((state / "report.md").read_text().startswith(
                "# INTERIM director model study — 2026-10-06 19:00 PDT"))
            names = ("turns.jsonl", "compactions.jsonl", "extract.json", "compare.json",
                     "phases.json", "report.md", "history.jsonl")
            first = {name: (state / name).read_bytes() for name in names}
            self.assertEqual(run_cli("report", *report_flags)[0], 0)
            self.assertEqual({name: (state / name).read_bytes() for name in names}, first)
            self.assertEqual(len(read_lines(state / "history.jsonl")), 1)
            status, _ = run_cli("ready", *flags, "--now", SECOND)
            self.assertEqual(status, 3)
            self.assertEqual(read_json(state / "extract.json")["extracted"], "2026-10-07T03:00:00+00:00")

    def test_history_tracks_changed_sample_and_replaces_same_instant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare_report(root)
            first = comparison_fixture()
            later = copy.deepcopy(first)
            alpha = later["directors"][0]
            alpha["classes"]["sonnet"]["continuation"]["n"] = 75
            alpha["classes"]["sonnet"]["continuation"]["seconds_median"] = 8
            pooled = later["pooled"]
            assert pooled is not None
            pooled["classes"]["sonnet"]["continuation"]["n"] = 125
            pooled["classes"]["sonnet"]["continuation"]["seconds_median"] = 9
            phase_data = phase_fixture()
            with patch.object(model_study, "compare", side_effect=[first, later, later]), \
                 patch.object(model_study, "phases", return_value=phase_data):
                self.assertEqual(report_cli(root, FIRST, "--interim")[0], 0)
                first_report = (root / "state" / "report.md").read_text()
                self.assertIn("first run", first_report)
                self.assertEqual(report_cli(root, SECOND, "--interim")[0], 0)
                second_report = (root / "state" / "report.md").read_bytes()
                history_path = root / "state" / "history.jsonl"
                second_history = history_path.read_bytes()
                self.assertEqual(report_cli(root, SECOND, "--interim")[0], 0)
            self.assertEqual((root / "state" / "report.md").read_bytes(), second_report)
            self.assertEqual(history_path.read_bytes(), second_history)
            history = read_lines(history_path)
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["mode"], "interim")
            self.assertEqual(history[0]["at"], "2026-10-07T02:00:00+00:00")
            directors = cast(list[JsonRecord], history[1]["directors"])
            alpha_history = next(row for row in directors if row["name"] == "alpha")
            self.assertEqual(alpha_history["sonnet_continuation_n"], 75)
            self.assertEqual(alpha_history["change_percent"], -60)
            report = second_report.decode()
            self.assertIn("## Since the last run", report)
            self.assertIn("2026-10-06 19:00 PDT", report)
            self.assertIn("50", report)
            self.assertIn("75", report)
            self.assertIn("recommendation unchanged", report)

    def test_no_history_and_short_message_count_pooled_line_before_table_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare_report(root)
            comparison = comparison_fixture()
            names = [f"director_{index:02d}" for index in range(24)]
            comparison["directors"] = [director(name, "switched", 40, 50, 20, 10) for name in names]
            phase_data = phase_fixture()
            with patch.object(model_study, "compare", return_value=comparison), \
                 patch.object(model_study, "phases", return_value=phase_data):
                self.assertEqual(report_cli(root, FIRST, "--interim")[0], 0)
                history = (root / "state" / "history.jsonl").read_bytes()
                status, message = report_cli(root, SECOND, "--interim", "--message", "--no-history")
            self.assertEqual(status, 0)
            self.assertEqual((root / "state" / "history.jsonl").read_bytes(), history)
            lines = message.splitlines()
            self.assertLessEqual(len(lines), 60)
            self.assertTrue(any(line.startswith("Pooled switched: median change then ")
                                and ", now " in line for line in lines))
            table_start = lines.index("Per-director comparison:")
            table_end = next(index for index, line in enumerate(lines) if line.startswith("Limits: "))
            table = lines[table_start:table_end]
            included = [line.split("|")[1].strip() for line in table if line.startswith("| director_")]
            self.assertGreater(len(names) - len(included), 0)
            self.assertEqual(included, names[:len(included)])
            self.assertEqual(table[-1], f"… {len(names) - len(included)} more directors in report.md")

    def test_message_trims_measure_lines_when_sixty_directors_switched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare_report(root)
            comparison = comparison_fixture()
            names = [f"director_{index:02d}" for index in range(60)]
            comparison["directors"] = [
                director(name, "switched", 40, 50, 20, 10) for name in names
            ]
            phase_data = phase_fixture()
            with patch.object(model_study, "compare", return_value=comparison), \
                 patch.object(model_study, "phases", return_value=phase_data):
                self.assertEqual(report_cli(root, FIRST, "--interim")[0], 0)
                status, short = report_cli(root, SECOND, "--interim", "--message")
            self.assertEqual(status, 0)
            lines = short.splitlines()
            self.assertLessEqual(len(lines), 60)
            self.assertTrue(lines[0].startswith("From model-study-unit: INTERIM director model study"))
            verdict_at = lines.index("Verdict:")
            measures = lines[1:verdict_at]
            shown = [line.split(":", 1)[0] for line in measures if line.startswith("director_")]
            self.assertGreater(len(names) - len(shown), 0)
            self.assertEqual(shown, names[:len(shown)])
            self.assertEqual(measures[-2],
                             f"… {len(names) - len(shown)} more directors in report.md")
            self.assertTrue(any(line.startswith("Pooled switched: median continuation-turn")
                                for line in measures))
            self.assertTrue(any(line.startswith("Pooled switched: median change then ")
                                for line in measures))
            self.assertIn("Recommendation:", short)
            self.assertTrue(any(line.startswith("Limits: ") for line in lines))
            self.assertEqual(lines[-1], str(root / "state" / "report.md"))


class ResultsTests(unittest.TestCase):
    def test_default_results_extracts_live_transcript_at_requested_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            (root / "registry").mkdir()
            (root / "runs").mkdir()
            write_roster(root / "roster.json", ["one"])
            _ = write_transcript(project, transcript("2026-10-06T23:00", "one"), "one")
            docs = root / "docs"
            status, _ = run_cli(
                "results", "--state-dir", str(root / "state"),
                "--projects-dir", str(root / "projects"),
                "--registry-dir", str(root / "registry"),
                "--runs-dir", str(root / "runs"),
                "--roster", str(root / "roster.json"),
                "--docs-dir", str(docs), "--now", FIRST,
            )
            self.assertEqual(status, 0)
            self.assertEqual(read_json(root / "state" / "extract.json")["extracted"],
                             "2026-10-07T02:00:00+00:00")
            self.assertTrue((docs / "director-model-study-results.md").read_text().startswith(
                "Sample as of 2026-10-06 19:00 PDT"))
            self.assertEqual(len(read_lines(root / "state" / "history.jsonl")), 1)

    def test_transcript_secrets_do_not_enter_extraction_report_or_results_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "projects" / "synthetic"
            project.mkdir(parents=True)
            (root / "registry").mkdir()
            (root / "runs").mkdir()
            write_roster(root / "roster.json", ["one"])
            _ = write_transcript(project, transcript("2026-10-06T23:00", "one"), "one")
            self.assertEqual(extract_cli(root)[0], 0)
            flags = (
                "--state-dir", str(root / "state"),
                "--projects-dir", str(root / "projects"),
                "--registry-dir", str(root / "registry"),
                "--runs-dir", str(root / "runs"),
                "--roster", str(root / "roster.json"), "--now", FIRST,
            )
            self.assertEqual(run_cli("report", *flags, "--interim")[0], 0)
            docs = root / "docs"
            self.assertEqual(run_cli("results", *flags, "--docs-dir", str(docs))[0], 0)
            output_files = [*list((root / "state").iterdir()), *list(docs.iterdir())]
            self.assertTrue(output_files)
            for path in output_files:
                with self.subTest(file=path.name):
                    content = path.read_bytes()
                    for forbidden in (SECRET_PROMPT, SECRET_ARGUMENT, SECRET_PATH, str(root)):
                        self.assertNotIn(forbidden.encode(), content)

    def test_results_are_compact_bounded_and_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare_report(root)
            comparison = comparison_fixture()
            phase_data = phase_fixture()
            docs = root / "docs"
            with patch.object(model_study, "compare", side_effect=comparison_writer(comparison)), \
                 patch.object(model_study, "phases", side_effect=phases_writer(phase_data)):
                self.assertEqual(results_cli(root, docs, FIRST)[0], 0)
                markdown_path = docs / "director-model-study-results.md"
                json_path = docs / "director-model-study-results.json"
                first_markdown, first_json = markdown_path.read_bytes(), json_path.read_bytes()
                first_history = (root / "state" / "history.jsonl").read_bytes()
                self.assertEqual(results_cli(root, docs, FIRST)[0], 0)
                self.assertEqual(markdown_path.read_bytes(), first_markdown)
                self.assertEqual(json_path.read_bytes(), first_json)
                self.assertEqual((root / "state" / "history.jsonl").read_bytes(), first_history)
                alternate = root / "alternate"
                self.assertEqual(results_cli(root, alternate, FIRST, "--no-history")[0], 0)
            self.assertEqual((alternate / markdown_path.name).read_bytes(), first_markdown)
            self.assertEqual((alternate / json_path.name).read_bytes(), first_json)
            self.assertLess(len(first_json), 300_000)
            self.assertNotIn(b"\n  ", first_json)
            parsed = read_json(json_path)
            self.assertEqual(set(parsed), {"compare", "phases", "history"})
            self.assertEqual(len(cast(list[JsonRecord], parsed["history"])), 1)
            document = first_markdown.decode()
            self.assertTrue(document.startswith("Sample as of 2026-10-06 19:00 PDT"))
            headings = ["Question", "Data", "Method", "The user's measure", "Per-director comparison",
                        "Verdict", "Sample gate", "Learning", "Limits", "Since the last run", "How to re-run"]
            positions = [document.index(heading) for heading in headings]
            self.assertEqual(positions, sorted(positions))
            self.assertIn("alpha: 50 filtered Sonnet continuation requests", document)
            self.assertIn("150", document)
            self.assertIn("G1", document)
            self.assertIn("2026-10-07 10:30 PDT", document)
            self.assertIn("python3 scripts/model_study/model_study.py results", document)
            self.assertIn("~/.local/state/model-study/turns.jsonl", document)
            self.assertIn("Time: median continuation seconds ", document)
            self.assertIn("Tokens and cost: median output tokens per request ", document)
            self.assertIn("mean cost in dollars per request ", document)
            self.assertIn("Turns and repair rounds: requests per 1,000 words ", document)
            self.assertIn("repair rounds per phase ", document)
            self.assertIn("Compactions: seconds per active hour, Opus ", document)

            prior = read_lines(root / "state" / "history.jsonl")[0]
            older: list[JsonRecord] = []
            for day in range(1, 26):
                row = copy.deepcopy(prior)
                row["at"] = f"2026-09-{day:02d}T00:00:00+00:00"
                older.append(row)
            _ = (root / "state" / "history.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in [*older, prior]), encoding="utf-8",
            )
            with patch.object(model_study, "compare", side_effect=comparison_writer(comparison)), \
                 patch.object(model_study, "phases", side_effect=phases_writer(phase_data)):
                self.assertEqual(results_cli(root, docs, SECOND)[0], 0)
            self.assertEqual(len(read_lines(root / "state" / "history.jsonl")), 27)
            latest = read_json(json_path)
            bounded = cast(list[JsonRecord], latest["history"])
            self.assertEqual(len(bounded), 20)
            self.assertEqual(bounded[-1]["at"], "2026-10-07T03:00:00+00:00")

    def test_results_refuse_large_json_without_writing_either_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare_report(root)
            comparison = comparison_fixture()
            comparison["compactions"] *= 300
            docs = root / "docs"
            with patch.object(model_study, "compare", side_effect=comparison_writer(comparison)), \
                 patch.object(model_study, "phases", side_effect=phases_writer(phase_fixture())):
                status, _ = results_cli(root, docs, FIRST)
            self.assertEqual(status, 4)
            self.assertFalse((docs / "director-model-study-results.md").exists())
            self.assertFalse((docs / "director-model-study-results.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
