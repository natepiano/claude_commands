"""Contract tests for screenshot episodes and the command output."""

from __future__ import annotations

import json
import base64
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import zlib
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO, ClassVar, NotRequired, TypedDict, cast, override
from unittest import mock

from scripts.shot_report import shot_report as report_module
from scripts.shot_report.changes import MeasurementChange, ProductChange, append_change, read_changes, write_changes
from scripts.shot_report.episodes import (
    GAP_CATEGORIES, EpisodeTimeline, LegacyEvidenceUnavailable, NoObservablePath, NoneCited,
    OneCitedShot, SeveralCitedShots, TimelineUnavailable, read_episodes, write_episodes,
)
from scripts.shot_report.transcripts import AvailableTimingSource, ExactAttemptCountFromOrderedCaptures


class EpisodeRecord(TypedDict):
    split: int
    agent: str
    project: str
    method: str
    start: str
    end: str
    screenshot_count: int
    image_count: int
    hana_shot_call_count: int
    other_call_count: int
    transcript_path: str
    session_id: str
    source: list[str]
    kept_shot_state: NotRequired[str]
    attempts_before_first_kept_shot: NotRequired[int]


HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
COMMAND = HERE / "shot_report.py"


def _chunk_bytes(row: dict[str, object]) -> bytes:
    encoded = base64.b64decode(str(row["data"]), validate=True)
    return zlib.decompress(encoded) if row.get("encoding") == "zlib+base64" else encoded


def _chunk_record(key: str, content: bytes) -> dict[str, object]:
    return {"kind": "chunk", "key": key, "encoding": "zlib+base64",
            "data": base64.b64encode(zlib.compress(content)).decode("ascii")}


def _timeline_part(stamp: datetime, session: str, row_type: str,
                   part: dict[str, object]) -> dict[str, object]:
    return {
        "timestamp": stamp.isoformat(), "type": row_type, "cwd": "/fictional/studio",
        "sessionId": session, "message": {
            "role": "assistant" if row_type == "assistant" else "user", "content": [part],
        },
    }


def _write_duration_fixture(path: Path, start: datetime, duration_minutes: float) -> None:
    session = path.stem
    duration_seconds = round(duration_minutes * 60)
    rows = [
        _timeline_part(start, session, "assistant", {
            "type": "tool_use", "id": "first-shot", "name": "Bash",
            "input": {"command": "python3 hana_shot.py shot --view first"},
        }),
        _timeline_part(start + timedelta(seconds=30), session, "user", {
            "type": "tool_result", "tool_use_id": "first-shot", "content": "saved /tmp/first.png",
        }),
    ]
    for seconds in range(240, duration_seconds - 30, 240):
        use_id = f"bridge-{seconds}"
        rows.extend((
            _timeline_part(start + timedelta(seconds=seconds), session, "assistant", {
                "type": "tool_use", "id": use_id, "name": "mcp__brp__world_get_components",
                "input": {"entity": 1},
            }),
            _timeline_part(start + timedelta(seconds=seconds + 6), session, "user", {
                "type": "tool_result", "tool_use_id": use_id, "content": "{}",
            }),
        ))
    rows.extend((
        _timeline_part(start + timedelta(seconds=duration_seconds - 30), session, "assistant", {
            "type": "tool_use", "id": "last-shot", "name": "Bash",
            "input": {"command": "python3 hana_shot.py shot --view last"},
        }),
        _timeline_part(start + timedelta(seconds=duration_seconds), session, "user", {
            "type": "tool_result", "tool_use_id": "last-shot", "content": "saved /tmp/last.png",
        }),
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _long_episode_rows(output: str) -> list[dict[str, str]]:
    section = output.split("What fills long /hana_shot episodes", 1)[1]
    lines = section.splitlines()
    header = next(line for line in lines if line.startswith("host"))
    columns = re.split(r"\s{2,}", header.strip())
    expected_columns = [
        "host", "split_s", "threshold", "category", "long_min", "long_share", "median_long_min",
        "long_n", "short_min", "short_share", "median_short_min", "short_n",
    ]
    if columns != expected_columns:
        raise AssertionError(f"unexpected long-episode columns: {columns}")
    return [
        dict(zip(columns, cells, strict=True))
        for line in lines[lines.index(header) + 1:]
        if len(cells := re.split(r"\s{2,}", line.strip())) == len(columns)
        and cells[1] in {"300", "900"}
        and cells[2] in {"p75_5m", "10m+"}
    ]


class ShotReportTest(unittest.TestCase):
    state: ClassVar[tempfile.TemporaryDirectory[str]]
    state_dir: ClassVar[Path]
    records: ClassVar[list[EpisodeRecord]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.state = tempfile.TemporaryDirectory()
        cls.state_dir = Path(cls.state.name)
        result = cls.run_command(
            "scan",
            "--claude-root",
            str(FIXTURES / "claude" / "projects"),
            "--codex-root",
            str(FIXTURES / "codex" / "sessions"),
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)
        cls.records = cls.read_records()

    @classmethod
    @override
    def tearDownClass(cls) -> None:
        cls.state.cleanup()

    @classmethod
    def run_command(cls, *arguments: str) -> subprocess.CompletedProcess[str]:
        timing_args = ("--timings-path", str(cls.state_dir / "fixture-timings.jsonl")) if arguments[0] == "scan" else ()
        return subprocess.run(
            [sys.executable, str(COMMAND), *arguments, *timing_args, "--state-dir", str(cls.state_dir)],
            cwd=HERE,
            capture_output=True,
            text=True,
            check=False,
        )

    @classmethod
    def read_records(cls) -> list[EpisodeRecord]:
        path = cls.state_dir / "episodes.jsonl"
        return [
            cast(EpisodeRecord, json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def test_control_has_one_episode_per_method_and_agent(self) -> None:
        control = [
            record
            for record in self.records
            if record["split"] == 300 and record["session_id"].endswith("-control")
        ]
        self.assertEqual(len(control), 4, control)
        self.assertEqual(
            {(record["agent"], record["method"]) for record in control},
            {
                ("Claude", "by hand"),
                ("Claude", "/hana_shot"),
                ("Codex", "by hand"),
                ("Codex", "/hana_shot"),
            },
        )
        self.assertEqual({record["project"] for record in control}, {"project-orion"})
        by_hand = [record for record in control if record["method"] == "by hand"]
        self.assertEqual(len({tuple(record["source"]) for record in by_hand}), 2)

    def test_gap_uses_result_to_next_start_and_splits_only_above_limit(self) -> None:
        gap = [record for record in self.records if record["session_id"] == "claude-gap"]
        at_five = [record for record in gap if record["split"] == 300]
        at_fifteen = [record for record in gap if record["split"] == 900]
        self.assertEqual(len(at_five), 2)
        self.assertEqual([record["screenshot_count"] for record in at_five], [2, 1])
        self.assertEqual(len(at_fifteen), 1)
        self.assertEqual(at_fifteen[0]["screenshot_count"], 3)
        self.assertEqual(at_five[0]["start"], "2026-10-02T00:00:00+00:00")
        self.assertEqual(at_five[0]["end"], "2026-10-02T00:06:00+00:00")

    def test_skipped_brp_call_does_not_raise_other_call_count(self) -> None:
        control = next(
            record
            for record in self.records
            if record["split"] == 300
            and record["session_id"] == "claude-control"
            and record["method"] == "by hand"
        )
        self.assertEqual(control["screenshot_count"], 1)
        self.assertEqual(control["other_call_count"], 1)
        self.assertEqual(control["start"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(control["end"], "2026-10-01T00:01:05+00:00")

    def test_episode_file_has_both_splits_and_scan_replaces_it(self) -> None:
        self.assertEqual(len(self.records), 13)
        self.assertTrue((self.state_dir / "scan-cache.pickle").exists())
        self.assertIn("b01a299", {change.commit for change in read_changes(self.state_dir / "changes.json")})
        self.assertEqual({record["split"] for record in self.records}, {300, 900})
        for record in self.records:
            self.assertIn(record["agent"], {"Claude", "Codex"})
            self.assertIn(record["method"], {"by hand", "/hana_shot"})
            self.assertGreater(record["screenshot_count"], 0)
            self.assertTrue(record["transcript_path"].endswith(".jsonl"))
            self.assertIn("+00:00", record["start"])
            self.assertIn("+00:00", record["end"])
        result = self.run_command(
            "scan",
            "--claude-root",
            str(FIXTURES / "claude" / "projects"),
            "--codex-root",
            str(FIXTURES / "codex" / "sessions"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_records(), self.records)

    def test_episode_sources_round_trip_as_sorted_json_lists(self) -> None:
        stored = read_episodes(self.state_dir / "episodes.jsonl")
        target = self.state_dir / "roundtrip.jsonl"
        write_episodes(target, stored)
        self.assertEqual(read_episodes(target), stored)
        self.assertEqual(len(target.read_text(encoding="utf-8").splitlines()), 13)
        raw = [cast(dict[str, object], json.loads(line)) for line in target.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(isinstance(row["source"], list) for row in raw))
        self.assertTrue(all(row["source"] == sorted(cast(list[str], row["source"])) for row in raw))

    def test_old_source_string_and_missing_image_count_load(self) -> None:
        old: dict[str, object] = dict(self.records[0])
        old["source"] = ",".join(cast(list[str], old["source"]))
        _ = old.pop("image_count")
        _ = old.pop("hana_shot_call_count")
        _ = old.pop("kept_shot_state")
        target = self.state_dir / "legacy.jsonl"
        _ = target.write_text(json.dumps(old) + "\n", encoding="utf-8")
        loaded = read_episodes(target)
        self.assertEqual(len(loaded), 1)
        self.assertIsInstance(loaded[0].source, frozenset)
        self.assertEqual(loaded[0].image_count, loaded[0].screenshot_count)
        self.assertIsInstance(loaded[0].kept_shot, LegacyEvidenceUnavailable)

    def test_report_counts_each_kept_state_and_first_kept_attempts(self) -> None:
        baseline = next(episode for episode in read_episodes(self.state_dir / "episodes.jsonl")
                        if episode.split == 300)
        evidence = [OneCitedShot(2), SeveralCitedShots(1, 2), NoneCited(),
                    NoObservablePath(), LegacyEvidenceUnavailable()]
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            write_episodes(state_dir / "episodes.jsonl",
                           [replace(baseline, kept_shot=state,
                                    attempt_count_evidence=(ExactAttemptCountFromOrderedCaptures()
                                                            if index == 0 else baseline.attempt_count_evidence))
                            for index, state in enumerate(evidence)])
            with (state_dir / "episodes.jsonl").open("a", encoding="utf-8") as target:
                _ = target.write("{broken json\n")
                _ = target.write('{"start":"2026-10-01T00:00:00Z","end":"2026-10-01T00:00:01Z",' +
                                 '"method":"by hand","source":[]}\n')
            self.assertEqual(len(read_episodes(state_dir / "episodes.jsonl")), 5)
            self.assertIsInstance(read_episodes(state_dir / "episodes.jsonl")[0].attempt_count_evidence,
                                  ExactAttemptCountFromOrderedCaptures)
            result = subprocess.run([sys.executable, str(COMMAND), "report", "--state-dir", str(state_dir)],
                                    cwd=HERE, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        header = next(line for line in result.stdout.splitlines() if line.startswith("split_s"))
        self.assertIn("no_observable_path", header)
        self.assertIn("exact_first_kept_n", header)
        self.assertIn("inferred_first_kept_n", header)
        row = next(line for line in result.stdout.splitlines() if line.startswith("300 "))
        self.assertEqual(re.split(r"\s{2,}", row.strip())[-14:],
                         ["5", "1", "4", "1", "1", "1", "1", "1", "1", "2.0", "2.0", "1", "1.0", "1.0"])

    def test_hana_shot_episode_counts_distinct_images_and_calls(self) -> None:
        image_episode = next(
            record for record in self.records
            if record["split"] == 300 and record["session_id"] == "claude-images"
        )
        self.assertEqual(image_episode["screenshot_count"], 1)
        self.assertEqual(image_episode["image_count"], 2)
        self.assertEqual(image_episode["hana_shot_call_count"], 1)

    def test_report_places_both_splits_on_one_row_for_each_group(self) -> None:
        result = self.run_command(
            "report",
            "--since",
            "2026-09-30",
            "--until",
            "2026-10-03",
            "--project",
            "project-orion",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout
        for term in (
            "2026-09-30",
            "2026-10-03",
            "300",
            "900",
            "Claude",
            "Codex",
            "by hand",
            "/hana_shot",
            "project-orion",
            "median",
            "p75",
            "p90",
            "shots",
            "other",
        ):
            with self.subTest(term=term):
                self.assertIn(term, output)
        header = next(line for line in output.splitlines() if "n_5m" in line)
        self.assertIn("n_15m", header)
        self.assertLess(header.index("n_5m"), header.index("n_15m"))
        rows = [
            line.split() for line in output.splitlines()
            if "project-orion" in line and line.startswith(("by hand", "/hana_shot"))
        ]
        self.assertEqual(len(rows), 4)
        by_hand_claude = next(row for row in rows if row[:2] == ["by", "hand"] and row[2] == "Claude")
        self.assertEqual(by_hand_claude[4:6], ["3", "2"])

    def test_report_aligns_project_names_with_spaces(self) -> None:
        episodes = read_episodes(self.state_dir / "episodes.jsonl")
        by_hand = [
            replace(episode, project="project with spaces")
            for episode in episodes
            if episode.session_id == "claude-control" and episode.method == "by hand"
        ]
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            write_episodes(state_dir / "episodes.jsonl", by_hand)
            result = subprocess.run(
                [sys.executable, str(COMMAND), "report", "--state-dir", str(state_dir)],
                cwd=HERE, capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = next(line for line in result.stdout.splitlines() if line.startswith("by hand"))
        self.assertEqual(re.split(r"\s{2,}", row.strip())[:5], [
            "by hand", "Claude", "project with spaces", "1", "1",
        ])

    def test_report_shows_per_image_time_hana_totals_and_recent_manual_sessions(self) -> None:
        result = self.run_command("report", "--since", "2026-09-30", "--until", "2026-10-03")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout
        self.assertIn("Minutes per image, 5-min split; n_images counts images", output)
        self.assertIn("/hana_shot, 5-min split: 3 calls, 4 images", output)
        self.assertIn("By-hand sessions: 3; newest 20 shown", output)
        self.assertIn("Claude project-orion claude-gap", output)
        self.assertIn("Codex project-orion codex-control", output)
        self.assertLess(output.index("claude-gap"), output.index("codex-control"))
        image_section = output.split("Minutes per image, 5-min split; n_images counts images", 1)[1]
        image_row = next(
            line.split() for line in image_section.splitlines()
            if line.startswith("/hana_shot") and "Claude" in line
        )
        self.assertEqual(image_row[-4:], ["1.5", "1.5", "3", "2"])

    def test_known_gap_fixture_attributes_every_minute_and_names_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            scan = subprocess.run([
                sys.executable, str(COMMAND), "scan",
                "--claude-root", str(FIXTURES / "timeline" / "claude"),
                "--codex-root", str(state_dir / "codex"),
                "--timings-path", str(FIXTURES / "timeline" / "timings.jsonl"),
                "--state-dir", str(state_dir),
            ], cwd=HERE, capture_output=True, text=True, check=False)
            self.assertEqual(scan.returncode, 0, scan.stderr)
            report = subprocess.run([
                sys.executable, str(COMMAND), "report", "--state-dir", str(state_dir),
                "--since", "2026-09-30", "--until", "2026-10-02",
            ], cwd=HERE, capture_output=True, text=True, check=False)
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn("Window:", report.stdout)
        self.assertIn("natedev evidence covered through", report.stdout)
        self.assertIn("sources: Claude transcripts, Codex transcripts, timings", report.stdout)
        self.assertIn("Mac Claude transcripts: out", report.stdout)
        section_summary = report.stdout.split("What fills long /hana_shot episodes", 1)[1].splitlines()[1]
        self.assertIn("Window:", section_summary)
        self.assertIn("thresholds from 5-min split", section_summary)
        self.assertIn("host coverage: natedev through", section_summary)
        self.assertIn("Mac Claude transcripts: out", section_summary)
        rows = _long_episode_rows(report.stdout)
        self.assertEqual(list(dict.fromkeys(row["host"] for row in rows)), ["all", "natedev"])
        for host in ("all", "natedev"):
            for split in ("300", "900"):
                for threshold in ("p75_5m", "10m+"):
                    categories = {
                        row["category"] for row in rows
                        if row["host"] == host and row["split_s"] == split
                        and row["threshold"] == threshold
                    }
                    self.assertEqual(categories, set(GAP_CATEGORIES))

    def test_long_episode_rows_include_all_and_each_host_and_longest_list_names_host(self) -> None:
        durations = (4, 5, 6, 7, 8, 9, 9.5, 10, 11, 12, 13, 14, 20)
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            claude_root = state_dir / "claude"
            for index, duration in enumerate(durations):
                _write_duration_fixture(
                    claude_root / f"duration-{duration:g}.jsonl",
                    datetime.fromisoformat("2026-10-01T00:00:00+00:00") + timedelta(days=index),
                    duration,
                )
            scan = subprocess.run([
                sys.executable, str(COMMAND), "scan",
                "--claude-root", str(claude_root), "--codex-root", str(state_dir / "codex"),
                "--timings-path", str(state_dir / "missing-timings.jsonl"),
                "--state-dir", str(state_dir),
            ], cwd=HERE, capture_output=True, text=True, check=False)
            self.assertEqual(scan.returncode, 0, scan.stderr)
            episodes = read_episodes(state_dir / "episodes.jsonl")
            write_episodes(state_dir / "episodes.jsonl", [
                replace(episode, source_host=("mac" if episode.session_id.endswith(("10", "12", "14", "20"))
                                              else "natedev"))
                for episode in episodes
            ])
            status = report_module._read_object(state_dir / "scan_status.json")  # pyright: ignore[reportPrivateUsage]
            status["mac_claude"] = "out"
            report_module._save_object(state_dir / "scan_status.json", status)  # pyright: ignore[reportPrivateUsage]
            report = subprocess.run([
                sys.executable, str(COMMAND), "report", "--state-dir", str(state_dir),
            ], cwd=HERE, capture_output=True, text=True, check=False)
        self.assertEqual(report.returncode, 0, report.stderr)
        rows = _long_episode_rows(report.stdout)
        self.assertEqual(list(dict.fromkeys(row["host"] for row in rows)),
                         ["all", "natedev", "mac (Claude out)"])
        for host in ("all", "natedev", "mac (Claude out)"):
            self.assertEqual(len([row for row in rows if row["host"] == host]), 32)
        for split in ("300", "900"):
            p75 = next(row for row in rows if row["host"] == "all" and row["split_s"] == split
                       and row["threshold"] == "p75_5m" and row["category"] == "hana_shot")
            ten_minutes = next(row for row in rows if row["host"] == "all" and row["split_s"] == split
                               and row["threshold"] == "10m+" and row["category"] == "hana_shot")
            self.assertEqual((p75["long_n"], p75["short_n"]), ("4", "6"))
            self.assertEqual((ten_minutes["long_n"], ten_minutes["short_n"]), ("6", "6"))
            self.assertEqual(
                (p75["long_min"], p75["long_share"], p75["median_long_min"]),
                ("4.0", "6.8%", "1.0"),
            )
            self.assertEqual(
                (ten_minutes["long_min"], ten_minutes["long_share"], ten_minutes["median_long_min"]),
                ("6.0", "7.5%", "1.0"),
            )
            for row in (p75, ten_minutes):
                self.assertEqual(
                    (row["short_min"], row["short_share"], row["median_short_min"]),
                    ("6.0", "15.4%", "1.0"),
                )

        longest = report.stdout.split("10 longest /hana_shot episodes, 5-min split", 1)[1]
        header = next(line for line in longest.splitlines() if line.startswith("host agent project session_id"))
        self.assertEqual(header.split(), [
            "host", "agent", "project", "session_id", "start", "duration_min",
            "largest_category", "largest_min", "second_category", "second_min",
        ])
        episode_rows = [
            re.split(r"\s{2,}", line.strip())
            for line in longest.splitlines()[longest.splitlines().index(header) + 1:]
            if "duration-" in line
        ]
        self.assertEqual(len(episode_rows), 10)
        self.assertEqual([float(row[5]) for row in episode_rows], sorted(
            (duration for duration in durations if duration >= 7), reverse=True,
        ))
        self.assertEqual({row[0] for row in episode_rows}, {"natedev", "mac (Claude out)"})
        self.assertTrue(all(row[6] == "agent_time" and row[8] == "hana_shot" for row in episode_rows))

    def test_long_episode_section_excludes_records_without_episode_timeline_and_counts_them(self) -> None:
        baseline = next(
            episode for episode in read_episodes(self.state_dir / "episodes.jsonl")
            if episode.split == 300 and episode.method == "/hana_shot"
        )
        available = replace(baseline, source_host="natedev", timeline=EpisodeTimeline(()))
        unavailable = replace(
            available, session_id="saved-before-timelines",
            timeline=TimelineUnavailable(),
        )
        with_excluded = report_module.long_episode_report_rows(
            [available, unavailable], "all available", "latest saved", {}, "out",
        )
        output = "\n".join(with_excluded)
        self.assertIn("/hana_shot episodes left out for lacking a timeline: n=1", with_excluded[1])
        self.assertNotIn("saved-before-timelines", output)
        self.assertEqual({row["long_n"] for row in _long_episode_rows(output)
                          if row["host"] == "all" and row["split_s"] == "300"
                          and row["threshold"] == "p75_5m"}, {"1"})

        without_excluded = report_module.long_episode_report_rows(
            [available], "all available", "latest saved", {}, "out",
        )
        self.assertNotIn("left out for lacking a timeline", without_excluded[1])

    def test_cli_has_no_rebuild_timelines_command_or_helper(self) -> None:
        result = subprocess.run(
            [sys.executable, str(COMMAND), "--help"], cwd=HERE,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("rebuild-timelines", result.stdout)
        self.assertFalse(hasattr(report_module, "rebuild_saved_timelines"))

    def test_survey_shows_call_session_and_project_counts(self) -> None:
        result = self.run_command("survey")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source calls sessions projects counted_as_by_hand", result.stdout)
        self.assertIn("mcp_brp 4 2 1 yes", result.stdout)
        self.assertIn("bash_brp 1 1 1 yes", result.stdout)
        self.assertIn("hana_shot 3 3 1 yes", result.stdout)

    def test_changes_seed_once_and_preserve_approved_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changes.json"
            changes = read_changes(path)
            self.assertTrue(path.exists())
            self.assertIn("b01a299", {change.commit for change in changes})
            self.assertIn("dab07788", {change.commit for change in changes})
            recording = next(change for change in changes if change.commit == "e6c96fb")
            self.assertIsInstance(recording, MeasurementChange)
            added = ProductChange("claude", "approved123", "Faster crop", datetime.fromisoformat("2026-10-08T00:00:00+00:00"),
                                  ("natedev",))
            append_change(path, added)
            append_change(path, added)
            self.assertEqual(sum(change.commit == "approved123" for change in read_changes(path)), 1)

    def test_legacy_changes_load_as_named_variants_and_save_without_bool(self) -> None:
        stamp = "2026-10-08T00:00:00+00:00"
        legacy: list[dict[str, object]] = [
            {"repository": "claude", "commit": "product", "summary": "Crop", "effective_at": stamp,
             "host_coverage": ["natedev"]},
            {"repository": "claude", "commit": "measurement", "summary": "Count failures", "effective_at": stamp,
             "host_coverage": ["natedev"], "measurement_change": True},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changes.json"
            _ = path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = read_changes(path)
            self.assertEqual([(change.commit, type(change)) for change in loaded],
                             [("measurement", MeasurementChange), ("product", ProductChange)])
            write_changes(path, loaded)
            saved = cast(list[dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual({row["kind"] for row in saved}, {"product", "measurement"})
            self.assertTrue(all("measurement_change" not in row for row in saved))
            self.assertEqual(read_changes(path), loaded)

    def test_hourly_guard_skips_same_utc_hour_and_resumes_next_hour(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            calls: list[datetime] = []

            def fake_scan(state: Path, _claude: Path, _codex: Path,
                          _timing: object, _mac: bool) -> str:
                stamp = datetime.fromisoformat("2026-10-06T22:05:00+00:00") if not calls else datetime.fromisoformat("2026-10-06T23:05:00+00:00")
                calls.append(stamp)
                report_module._save_object(state / "scan_status.json", {"last_success": stamp.isoformat()})  # pyright: ignore[reportPrivateUsage]
                return "scanned"

            with mock.patch.object(report_module, "scan", side_effect=fake_scan):
                self.assertEqual(report_module.scan_hourly(state_dir, datetime.fromisoformat("2026-10-06T22:05:00+00:00")), "scanned")
                self.assertIn("skipped", report_module.scan_hourly(state_dir, datetime.fromisoformat("2026-10-06T22:45:00+00:00")))
                self.assertEqual(report_module.scan_hourly(state_dir, datetime.fromisoformat("2026-10-06T23:05:00+00:00")), "scanned")
            self.assertEqual(len(calls), 2)

    def test_change_windows_show_both_splits_and_incomplete_host(self) -> None:
        baseline = next(episode for episode in read_episodes(self.state_dir / "episodes.jsonl") if episode.split == 300)
        change_time = datetime.fromisoformat("2026-10-01T00:30:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            records = [
                replace(baseline, split=split, method=method, source_host="natedev",
                        start=change_time + delta, end=change_time + delta + timedelta(minutes=2))
                for split in (300, 900) for method, delta in (
                    ("by hand", timedelta(minutes=-15)), ("/hana_shot", timedelta(minutes=15)))
            ]
            write_episodes(state_dir / "episodes.jsonl", records)
            write_changes(state_dir / "changes.json", [
                ProductChange("claude", "rollout", "Introduce /hana_shot", change_time, ("natedev", "mac")),
                MeasurementChange("claude", "recording", "Log calls", change_time + timedelta(hours=1), ("natedev",)),
            ])
            report_module._save_object(state_dir / "scan_status.json", {  # pyright: ignore[reportPrivateUsage]
                "last_success": "2026-10-02T00:00:00+00:00",
                "host_last_success": {"natedev": "2026-10-02T00:00:00+00:00"},
            })
            output = report_module.report(state_dir)
        self.assertIn("rollout by hand 300s before", output)
        self.assertIn("rollout /hana_shot 900s after", output)
        self.assertIn("too small to judge", output)
        self.assertIn("incomplete:mac", output)
        self.assertIn("Measurement change claude recording", output)
        self.assertIn("Weekly agent-hours", output)

    def test_invocation_table_counts_failures_without_transcript_and_attempt_phases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            records: list[dict[str, object]] = [
                {"time": "2026-10-01T00:00:00+00:00", "status": "success", "invocation_kind": "shot",
                 "attempts": [{"status": "success", "mode": "fit", "crop": "rect", "resolve_ms": 10, "total_ms": 100},
                              {"status": "failure", "failure_reason": "empty_crop", "image_paths": []}]},
                {"time": "2026-10-01T00:01:00+00:00", "status": "failure", "failure_reason": "timeout",
                 "invocation_kind": "shot", "attempts": []},
                {"time": "2026-10-01T00:02:00+00:00", "invocation_kind": "unknown", "mode": "pose",
                 "crop": "none", "total_ms": 200},
                {"time": "2026-10-01T00:03:00+00:00", "status": "success", "invocation_kind": "views_check",
                 "attempts": [{"status": "success", "mode": "fit", "crop": "none", "total_ms": 90}]},
            ]
            _ = (state_dir / "invocations.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            output = report_module.report(state_dir, since="2026-09-30", until="2026-10-02")
        self.assertIn("kind=shot n=2 success=1 failure=1 legacy_success=0", output)
        self.assertIn("kind=views_check n=1 success=1", output)
        self.assertIn("kind=unknown n=1 success=0 failure=0 legacy_success=1", output)
        self.assertIn("failure_reason=timeout n=1", output)
        self.assertIn("failure_reason=empty_crop n=1", output)
        self.assertIn("failure_reason=shot_failed n=0", output)
        self.assertIn("kind=shot mode=fit crop=rect n=1 resolve_ms=10.0(n=1)", output)

    def test_winter_window_names_pst_with_offset(self) -> None:
        stamp = datetime.fromisoformat("2026-01-10T12:00:00+00:00")
        self.assertIn("PST (-0800)", report_module._pacific(stamp))  # pyright: ignore[reportPrivateUsage]

    def test_winter_report_heading_names_zone_and_both_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            heading = report_module.report(Path(directory), "2026-01-10", "2026-01-11").splitlines()[0]
        self.assertIn("America/Los_Angeles", heading)
        self.assertEqual(heading.count("PST (-0800)"), 2)

    def test_change_window_stops_at_other_repository_change_on_same_host(self) -> None:
        baseline = next(episode for episode in read_episodes(self.state_dir / "episodes.jsonl") if episode.split == 300)
        rollout = datetime.fromisoformat("2026-10-01T00:30:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            write_episodes(state_dir / "episodes.jsonl", [
                replace(baseline, project="project-a", source_host="natedev", method="by hand",
                        start=rollout - timedelta(minutes=5), end=rollout - timedelta(minutes=4)),
                replace(baseline, project="project-b", source_host="natedev", method="/hana_shot",
                        start=rollout + timedelta(minutes=15), end=rollout + timedelta(minutes=16)),
                replace(baseline, project="project-c", source_host="natedev", method="/hana_shot",
                        start=rollout + timedelta(minutes=25), end=rollout + timedelta(minutes=26)),
            ])
            write_changes(state_dir / "changes.json", [
                ProductChange("claude", "rollout", "Introduce /hana_shot", rollout, ("natedev",)),
                ProductChange("claude", "mac-only", "Mac display", rollout + timedelta(minutes=10), ("mac",)),
                ProductChange("bevy_brp", "crop", "Crop in extras", rollout + timedelta(minutes=20), ("natedev",)),
            ])
            report_module._save_object(state_dir / "scan_status.json", {  # pyright: ignore[reportPrivateUsage]
                "host_last_success": {"natedev": "2026-10-02T00:00:00+00:00"},
            })
            output = report_module.report(state_dir)
        row = next(line for line in output.splitlines()
                   if line.startswith("claude rollout /hana_shot 300s after host=natedev"))
        self.assertIn("n=1 median_min=1.0", row)
        self.assertIn(report_module._pacific(rollout + timedelta(minutes=20)), row)  # pyright: ignore[reportPrivateUsage]
        self.assertNotIn(report_module._pacific(rollout + timedelta(minutes=10)), row)  # pyright: ignore[reportPrivateUsage]

    def test_mac_reader_filters_nonmatching_files_and_reexamines_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / ".codex/sessions"
            root.mkdir(parents=True)
            candidate = root / "candidate.jsonl"
            ordinary = root / "ordinary.jsonl"
            _ = candidate.write_bytes(b'{"type":"session_meta"}\n{"cmd":"hana_shot.py shot"}\n')
            _ = ordinary.write_bytes(b'{"type":"session_meta"}\n{"message":"hello"}\n')
            source = report_module._mac_reader_source()  # pyright: ignore[reportPrivateUsage]

            def read(prior: dict[str, object]) -> list[dict[str, object]]:
                result = subprocess.run([sys.executable, "-c", source], input=json.dumps(prior),
                                        capture_output=True, text=True, timeout=10, check=False,
                                        env={**os.environ, "HOME": directory})
                self.assertEqual(result.returncode, 0, result.stderr)
                return [cast(dict[str, object], json.loads(line)) for line in result.stdout.splitlines()]

            first = read({})
            files = {str(row["key"]): row for row in first if row.get("kind") == "file"}
            self.assertIs(files["codex/candidate.jsonl"]["matched"], True)
            self.assertIs(files["codex/ordinary.jsonl"]["matched"], False)
            self.assertEqual({row["key"] for row in first if row.get("kind") == "chunk"},
                             {"codex/candidate.jsonl"})
            prior: dict[str, object] = {}
            for key, row in files.items():
                received = sum(len(_chunk_bytes(chunk)) for chunk in first
                               if chunk.get("kind") == "chunk" and chunk.get("key") == key)
                prior[key] = {"size": row["size"], "inode": row["inode"], "mtime": row["mtime"],
                              "matched": row["matched"], "cursor": int(str(row["start"])) + received}
            second = read(prior)
            self.assertFalse(any(row.get("kind") == "chunk" for row in second))
            with ordinary.open("ab") as stream:
                _ = stream.write(b'{"cmd":"hana_shot.py shot"}\n')
            third = read(prior)
            changed = next(row for row in third if row.get("kind") == "file"
                           and row.get("key") == "codex/ordinary.jsonl")
            self.assertIs(changed["matched"], True)
            self.assertEqual(changed["start"], 0)
            sent = b"".join(_chunk_bytes(row) for row in third
                            if row.get("kind") == "chunk" and row.get("key") == "codex/ordinary.jsonl")
            self.assertEqual(sent, ordinary.read_bytes())

    def test_mac_reader_budget_stops_before_new_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".codex/sessions"
            root.mkdir(parents=True)
            _ = (root / "capture.jsonl").write_text('{"cmd":"hana_shot.py shot"}\n')
            result = subprocess.run([sys.executable, "-c", report_module._mac_reader_source(0)],  # pyright: ignore[reportPrivateUsage]
                                    input="{}", capture_output=True, text=True, timeout=10, check=False,
                                    env={**os.environ, "HOME": directory})
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [cast(dict[str, object], json.loads(line)) for line in result.stdout.splitlines()]
        self.assertFalse(any(record.get("kind") == "file" for record in records))
        self.assertEqual(records[-1], {"kind": "end", "finished": False})

    def test_mac_ssh_timeout_keeps_fifteen_seconds_beyond_reader_budget(self) -> None:
        budget = report_module.MAC_READ_BUDGET_SECONDS
        timeout = report_module.MAC_SSH_TIMEOUT_SECONDS
        self.assertGreaterEqual(timeout - budget, 15)
        self.assertEqual(timeout - budget, report_module.MAC_SSH_TIMEOUT_MARGIN_SECONDS)

    def test_default_mac_reader_uses_shared_budget(self) -> None:
        reader = report_module._mac_reader_source()  # pyright: ignore[reportPrivateUsage]
        self.assertIn(f"deadline = time.monotonic() + {report_module.MAC_READ_BUDGET_SECONDS}", reader)
        self.assertNotIn("__BUDGET_SECONDS__", reader)

    def test_mac_chunk_decoder_round_trips_each_encoding_and_rejects_unknown(self) -> None:
        content = b'{"cmd":"hana_shot.py shot"}\n' + bytes(range(256)) * 8
        for encoding, payload in (("base64", content), ("zlib+base64", zlib.compress(content))):
            with self.subTest(encoding=encoding):
                row: dict[str, object] = {"encoding": encoding,
                                          "data": base64.b64encode(payload).decode("ascii")}
                self.assertEqual(report_module._chunk_source_bytes(row), content)  # pyright: ignore[reportPrivateUsage]
                if encoding == "base64":
                    _ = row.pop("encoding")
                    self.assertEqual(report_module._chunk_source_bytes(row), content)  # pyright: ignore[reportPrivateUsage]
        with self.assertRaisesRegex(ValueError, "encoding"):
            _ = report_module._chunk_source_bytes({"encoding": "unknown", "data": "YQ=="})  # pyright: ignore[reportPrivateUsage]

    def test_mac_reader_uses_scan_filter_constants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".codex/sessions"
            root.mkdir(parents=True)
            _ = (root / "custom.jsonl").write_text('{"message":"custom_capture_marker"}\n')
            with mock.patch.object(report_module, "CODEX_PREFILTER", ("custom_capture_marker",)):
                source = report_module._mac_reader_source()  # pyright: ignore[reportPrivateUsage]
            result = subprocess.run([sys.executable, "-c", source], input="{}", capture_output=True,
                                    text=True, timeout=10, check=False, env={**os.environ, "HOME": directory})
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [cast(dict[str, object], json.loads(line)) for line in result.stdout.splitlines()]
        self.assertTrue(any(record.get("kind") == "chunk" and record.get("key") == "codex/custom.jsonl"
                            for record in records))

    def test_mac_reader_chunks_round_trip_and_count_source_bytes(self) -> None:
        content = b'{"cmd":"hana_shot.py shot"}\n' + b'x' * (2 * 65536 + 17)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / ".codex/sessions/capture.jsonl"
            source.parent.mkdir(parents=True)
            _ = source.write_bytes(content)
            clock = "import time\ntime.monotonic = lambda: 0\n"
            result = subprocess.run([sys.executable, "-c", clock + report_module._mac_reader_source()],  # pyright: ignore[reportPrivateUsage]
                                    input="{}", capture_output=True, text=True, timeout=10, check=False,
                                    env={**os.environ, "HOME": directory})
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [cast(dict[str, object], json.loads(line)) for line in result.stdout.splitlines()]
        chunks = [row for row in rows if row.get("kind") == "chunk"]
        self.assertTrue(chunks)
        self.assertTrue(all(row.get("encoding") == "zlib+base64" for row in chunks))
        source_chunks = [_chunk_bytes(row) for row in chunks]
        self.assertTrue(all(chunk for chunk in source_chunks))
        self.assertEqual(sum(map(len, source_chunks)), len(content))
        self.assertEqual(b"".join(source_chunks), content)

    def test_mac_reader_resumes_legacy_manifest_then_restarts_same_size_rewrite(self) -> None:
        first_content = b'{"cmd":"hana_shot.py shot"}\n' + b'a' * (65536 + 23)
        rewritten = b'{"cmd":"hana_shot.py shot"}\n' + b'b' * (65536 + 23)
        key = "codex/2026/10/06/capture.jsonl"
        real_run = subprocess.run
        sent_priors: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "remote"
            source = home / ".codex/sessions/2026/10/06/capture.jsonl"
            source.parent.mkdir(parents=True)
            _ = source.write_bytes(first_content)
            state_dir = Path(directory) / "state"
            mirror = state_dir / "mac-source" / key
            mirror.parent.mkdir(parents=True)
            cursor = 31000
            _ = mirror.write_bytes(first_content[:cursor])
            stat = source.stat()
            report_module._save_object(state_dir / "mac_manifest.json", {  # pyright: ignore[reportPrivateUsage]
                key: {"size": stat.st_size, "inode": stat.st_ino, "mtime": stat.st_mtime_ns,
                      "matched": True, "cursor": cursor},
            })

            def local_ssh(args: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
                self.assertEqual(args[0], "ssh")
                sent_priors.append(cast(dict[str, object], json.loads(cast(bytes, options["input"]))))
                command = shlex.shlex(args[-1], posix=True, punctuation_chars=";")
                command.whitespace_split = True
                reader = list(command)[2]
                clock = "import time\ntime.monotonic = lambda: 0\n"
                result = real_run([sys.executable, "-c", clock + reader],
                                  input=cast(bytes, options["input"]), stdout=cast(BinaryIO, options["stdout"]),
                                  stderr=subprocess.PIPE, timeout=10, check=False,
                                  env={**os.environ, "HOME": str(home)})
                return subprocess.CompletedProcess(args, result.returncode,
                                                   stderr=f"rc={result.returncode}\n".encode() + result.stderr)

            with mock.patch.object(subprocess, "run", side_effect=local_ssh):
                resumed = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
                unchanged = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(resumed, report_module.MacEvidenceAvailable)
            self.assertIsInstance(unchanged, report_module.MacEvidenceAvailable)
            assert isinstance(resumed, report_module.MacEvidenceAvailable)
            assert isinstance(unchanged, report_module.MacEvidenceAvailable)
            self.assertEqual(cast(dict[str, object], sent_priors[0][key])["cursor"], cursor)
            self.assertEqual(resumed.bytes_read, len(first_content) - cursor)
            self.assertEqual(unchanged.bytes_read, 0)
            self.assertEqual(mirror.read_bytes(), first_content)

            _ = source.write_bytes(rewritten)
            changed_ns = stat.st_mtime_ns + 2_000_000_000
            os.utime(source, ns=(changed_ns, changed_ns))
            with mock.patch.object(subprocess, "run", side_effect=local_ssh):
                replaced = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(replaced, report_module.MacEvidenceAvailable)
            assert isinstance(replaced, report_module.MacEvidenceAvailable)
            self.assertEqual(replaced.bytes_read, len(rewritten))
            self.assertEqual(mirror.read_bytes(), rewritten)
            manifest = report_module._read_object(state_dir / "mac_manifest.json")  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(cast(dict[str, object], manifest[key])["cursor"], len(rewritten))

    def test_previous_reader_manifest_resumes_without_duplicate_calls(self) -> None:
        content = (FIXTURES / "codex/sessions/2026/10/01/rollout-control.jsonl").read_bytes()
        key = "codex/2026/10/01/rollout-control.jsonl"
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "remote"
            source = home / ".codex/sessions/2026/10/01/rollout-control.jsonl"
            source.parent.mkdir(parents=True)
            _ = source.write_bytes(content)
            state_dir = Path(directory) / "state"
            mirror = state_dir / "mac-source" / key
            mirror.parent.mkdir(parents=True)
            cursor = len(content) // 2
            _ = mirror.write_bytes(content[:cursor])
            stat = source.stat()
            report_module._save_object(state_dir / "mac_manifest.json", {  # pyright: ignore[reportPrivateUsage]
                key: {"size": stat.st_size, "inode": stat.st_ino, "mtime": stat.st_mtime_ns,
                      "matched": True, "cursor": cursor},
            })

            def local_ssh(args: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
                command = shlex.shlex(args[-1], posix=True, punctuation_chars=";")
                command.whitespace_split = True
                reader = list(command)[2]
                clock = "import time\ntime.monotonic = lambda: 0\n"
                result = real_run([sys.executable, "-c", clock + reader],
                                  input=cast(bytes, options["input"]), stdout=cast(BinaryIO, options["stdout"]),
                                  stderr=subprocess.PIPE, timeout=10, check=False,
                                  env={**os.environ, "HOME": str(home)})
                return subprocess.CompletedProcess(args, result.returncode,
                                                   stderr=f"rc={result.returncode}\n".encode() + result.stderr)

            with mock.patch.object(subprocess, "run", side_effect=local_ssh):
                first = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
                second = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
            assert isinstance(first, report_module.MacEvidenceAvailable)
            assert isinstance(second, report_module.MacEvidenceAvailable)
            self.assertEqual((first.bytes_read, second.bytes_read), (len(content) - cursor, 0))
            self.assertEqual(mirror.read_bytes(), content)
            manifest = report_module._read_object(state_dir / "mac_manifest.json")  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(cast(dict[str, object], manifest[key])["cursor"], len(content))
            with mock.patch.object(report_module, "_mac_evidence", return_value=first):
                _ = report_module.scan(state_dir, state_dir / "local-claude", state_dir / "local-codex",
                                       include_mac=True)
            episodes = read_episodes(state_dir / "episodes.jsonl")
            self.assertTrue(episodes)
            with mock.patch.object(report_module, "_mac_evidence", return_value=second):
                _ = report_module.scan(state_dir, state_dir / "local-claude", state_dir / "local-codex",
                                       include_mac=True)
            self.assertEqual(read_episodes(state_dir / "episodes.jsonl"), episodes)

    def test_mac_reader_resumes_unchanged_file_after_each_budget(self) -> None:
        content = (b'{"cmd":"hana_shot.py shot"}\n' + b'{"message":"' +
                   b"x" * (3 * 65536 + 7) + b'"}\n')
        key = "codex/2026/10/06/large.jsonl"
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "remote"
            source = home / ".codex/sessions/2026/10/06/large.jsonl"
            source.parent.mkdir(parents=True)
            _ = source.write_bytes(content)
            state_dir = Path(directory) / "state"

            def local_ssh(args: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
                self.assertEqual(args[0], "ssh")
                command = shlex.shlex(args[-1], posix=True, punctuation_chars=";")
                command.whitespace_split = True
                reader = list(command)[2]
                tick = report_module.MAC_READ_BUDGET_SECONDS // 2 + 1
                clock = ("import time\n"
                         f"ticks = iter(range({tick}, {tick * 100}, {tick}))\n"
                         "time.monotonic = lambda: next(ticks)\n")
                result = real_run([sys.executable, "-c", clock + reader],
                                  input=cast(bytes, options["input"]), stdout=cast(BinaryIO, options["stdout"]),
                                  stderr=subprocess.PIPE, timeout=10, check=False,
                                  env={**os.environ, "HOME": str(home)})
                return subprocess.CompletedProcess(args, result.returncode,
                                                   stderr=f"rc={result.returncode}\n".encode() + result.stderr)

            received = 0
            for _ in range(8):
                with mock.patch.object(subprocess, "run", side_effect=local_ssh):
                    evidence = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(evidence, report_module.MacEvidenceAvailable)
                assert isinstance(evidence, report_module.MacEvidenceAvailable)
                self.assertEqual(evidence.bytes_read, min(65536, len(content) - received))
                received += evidence.bytes_read
                self.assertEqual((state_dir / "mac-source" / key).read_bytes(), content[:received])
                manifest = report_module._read_object(state_dir / "mac_manifest.json")  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(cast(dict[str, object], manifest[key])["cursor"], received)
                with mock.patch.object(report_module, "_mac_evidence", return_value=evidence):
                    line = report_module.scan(state_dir, state_dir / "local-claude",
                                              state_dir / "local-codex", include_mac=True)
                if isinstance(evidence.progress, report_module.MacReadFinished):
                    self.assertIn(f"Mac: finished; read {evidence.bytes_read} source bytes", line)
                    self.assertEqual(report_module._host_coverage(state_dir)["mac"].at,  # pyright: ignore[reportPrivateUsage]
                                     evidence.started_at)
                    break
                self.assertIn("catching up", line)
                self.assertNotIn("mac", report_module._host_coverage(state_dir))  # pyright: ignore[reportPrivateUsage]
            else:
                self.fail("Mac reader never finished the unchanged source file")
            self.assertEqual(received, len(content))
            self.assertEqual((state_dir / "mac-source" / key).read_bytes(), source.read_bytes())

    def test_mac_unreachable_then_reachable_catches_up_without_duplicates(self) -> None:
        fixture = FIXTURES / "codex/sessions/2026/10/01/rollout-control.jsonl"
        transcript = fixture.read_bytes()
        timing = (json.dumps({
            "time": "2026-10-01T00:30:09+00:00", "status": "success", "exit_code": 0,
            "invocation_kind": "shot", "session": {"state": "present", "value": "codex-control"},
            "attempts": [{"status": "success", "image_paths": ["/fictional/shots/codex-framed.png"],
                          "mode": "fit", "crop": "none", "total_ms": 50}],
        }) + "\n").encode()
        paths = {"codex/2026/10/01/rollout-control.jsonl": transcript,
                 "timings/timings.jsonl": timing}
        commands: list[list[str]] = []
        reachable = False

        def fake_ssh(args: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
            nonlocal reachable
            commands.append(args)
            self.assertIn("rc=$?", args[-1])
            self.assertEqual(options["timeout"], report_module.MAC_SSH_TIMEOUT_SECONDS)
            if not reachable:
                reachable = True
                return subprocess.CompletedProcess(args, 255, stderr=b"unreachable")
            output = cast(BinaryIO, options["stdout"])
            _ = output.write((json.dumps({"kind": "claude_coverage", "included": False}) + "\n").encode())
            prior = cast(dict[str, object], json.loads(cast(bytes, options["input"])))
            for key, content in paths.items():
                old = prior.get(key)
                meta = {"size": len(content), "inode": 11 if key.startswith("codex") else 12,
                        "mtime": 1 if len(content) == (len(transcript) if key.startswith("codex") else len(timing)) else 2}
                previous = cast(dict[str, object], old) if isinstance(old, dict) else {}
                start = (int(str(previous.get("cursor", 0))) if previous.get("inode") == meta["inode"]
                         and len(content) >= int(str(previous.get("cursor", 0))) else 0)
                _ = output.write((json.dumps({"kind": "file", "key": key, **meta,
                                              "matched": True, "start": start}) + "\n").encode())
                if start < len(content):
                    _ = output.write((json.dumps(_chunk_record(key, content[start:])) + "\n").encode())
            _ = output.write(b'{"kind":"end","finished":true}\n')
            return subprocess.CompletedProcess(args, 0, stderr=b"rc=0\n")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            empty_claude, empty_codex = state_dir / "local-claude", state_dir / "local-codex"
            with mock.patch.object(subprocess, "run", side_effect=fake_ssh):
                unavailable = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
                self.assertIsInstance(unavailable, report_module.MacEvidenceUnavailable)
                self.assertFalse((state_dir / "mac_manifest.json").exists())
                available = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
                assert isinstance(available, report_module.MacEvidenceAvailable)
                self.assertEqual(available.bytes_read, len(transcript) + len(timing))
                no_news = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
                assert isinstance(no_news, report_module.MacEvidenceAvailable)
                self.assertEqual(no_news.bytes_read, 0)
            with mock.patch.object(report_module, "_mac_evidence", return_value=available):
                _ = report_module.scan(state_dir, empty_claude, empty_codex, include_mac=True)
                first = read_episodes(state_dir / "episodes.jsonl")
            first_invocations = (state_dir / "invocations.jsonl").read_text()
            first_coverage = report_module._host_coverage(state_dir)["mac"].at  # pyright: ignore[reportPrivateUsage]
            with mock.patch.object(report_module, "_mac_evidence",
                                   return_value=report_module.MacEvidenceUnavailable("asleep")):
                outage_line = report_module.scan(state_dir, empty_claude, empty_codex, include_mac=True)
            self.assertEqual(read_episodes(state_dir / "episodes.jsonl"), first)
            self.assertEqual((state_dir / "invocations.jsonl").read_text(), first_invocations)
            self.assertEqual(report_module._host_coverage(state_dir)["mac"].at, first_coverage)  # pyright: ignore[reportPrivateUsage]
            self.assertIn("Mac: unavailable", outage_line)
            extra = "".join(json.dumps(row) + "\n" for row in (
                {"timestamp": "2026-10-01T01:30:00.000Z", "type": "response_item",
                 "payload": {"type": "function_call", "name": "exec_command",
                             "arguments": json.dumps({"cmd": "python3 /fictional/hana_shot.py shot --view orion"}),
                             "call_id": "codex-new"}},
                {"timestamp": "2026-10-01T01:30:10.000Z", "type": "response_item",
                 "payload": {"type": "function_call_output", "call_id": "codex-new",
                             "output": "saved /fictional/shots/codex-new.png"}},
            )).encode()
            extra_timing = (json.dumps({
                "time": "2026-10-01T01:30:10+00:00", "status": "success", "exit_code": 0,
                "invocation_kind": "shot", "session": {"state": "present", "value": "codex-control"},
                "attempts": [{"status": "success", "image_paths": ["/fictional/shots/codex-new.png"]}],
            }) + "\n").encode()
            paths["codex/2026/10/01/rollout-control.jsonl"] += extra
            paths["timings/timings.jsonl"] += extra_timing
            with (state_dir / "mac-source/codex/2026/10/01/rollout-control.jsonl").open("ab") as partial:
                _ = partial.write(b"interrupted transfer")
            with mock.patch.object(subprocess, "run", side_effect=fake_ssh):
                caught_up = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
            assert isinstance(caught_up, report_module.MacEvidenceAvailable)
            self.assertEqual(caught_up.bytes_read, len(extra) + len(extra_timing))
            self.assertEqual((state_dir / "mac-source/codex/2026/10/01/rollout-control.jsonl").read_bytes(),
                             paths["codex/2026/10/01/rollout-control.jsonl"])
            with mock.patch.object(report_module, "_mac_evidence", return_value=caught_up):
                second_scan = report_module.scan(state_dir, empty_claude, empty_codex, include_mac=True)
                second = read_episodes(state_dir / "episodes.jsonl")
                no_new_scan = report_module.scan(state_dir, empty_claude, empty_codex, include_mac=True)
            self.assertEqual(second, read_episodes(state_dir / "episodes.jsonl"))
            self.assertTrue(first)
            self.assertEqual(len(second), len(first) + 2)
            self.assertEqual({episode.source_host for episode in first}, {"mac"})
            self.assertEqual(len((state_dir / "invocations.jsonl").read_text().splitlines()), 2)
            self.assertIn("read 0 transcript and timing bytes", no_new_scan)
            self.assertNotIn("read 0 transcript and timing bytes", second_scan)
        self.assertEqual(len(commands), 4)

    def test_mac_budget_batch_commits_received_cursor_without_advancing_coverage(self) -> None:
        payload = b'{"cmd":"hana_shot.py shot"}\n'
        key = "codex/2026/10/01/capture.jsonl"
        calls = 0

        def fake_ssh(args: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
            nonlocal calls
            calls += 1
            output = cast(BinaryIO, options["stdout"])
            prior = cast(dict[str, object], json.loads(cast(bytes, options["input"])))
            old = prior.get(key)
            previous = cast(dict[str, object], old) if isinstance(old, dict) else {}
            start = int(str(previous.get("cursor", 0)))
            part = payload[start:8] if calls == 1 else payload[start:]
            rows: list[dict[str, object]] = [
                {"kind": "claude_coverage", "included": False},
                {"kind": "file", "key": key, "size": len(payload), "inode": 17, "mtime": 1,
                 "matched": True, "start": start},
                _chunk_record(key, part),
                {"kind": "end", "finished": calls > 1},
            ]
            for row in rows:
                _ = output.write((json.dumps(row) + "\n").encode())
            return subprocess.CompletedProcess(args, 0, stderr=b"rc=0\n")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch.object(subprocess, "run", side_effect=fake_ssh):
                first = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
            assert isinstance(first, report_module.MacEvidenceAvailable)
            self.assertIsInstance(first.progress, report_module.MacReadCatchingUp)
            self.assertEqual(first.bytes_read, 8)
            first_manifest = report_module._read_object(state_dir / "mac_manifest.json")  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(cast(dict[str, object], first_manifest[key])["cursor"], 8)
            with mock.patch.object(report_module, "_mac_evidence", return_value=first):
                first_scan = report_module.scan(state_dir, state_dir / "claude", state_dir / "codex", include_mac=True)
            self.assertIn("catching up", first_scan)
            self.assertNotIn("mac", report_module._host_coverage(state_dir))  # pyright: ignore[reportPrivateUsage]
            with mock.patch.object(subprocess, "run", side_effect=fake_ssh):
                second = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
            assert isinstance(second, report_module.MacEvidenceAvailable)
            self.assertIsInstance(second.progress, report_module.MacReadFinished)
            self.assertEqual(second.bytes_read, len(payload) - 8)
            self.assertEqual((state_dir / "mac-source" / key).read_bytes(), payload)
            with mock.patch.object(report_module, "_mac_evidence", return_value=second):
                second_scan = report_module.scan(state_dir, state_dir / "claude", state_dir / "codex", include_mac=True)
            self.assertIn("finished", second_scan)
            self.assertEqual(report_module._host_coverage(state_dir)["mac"].at,  # pyright: ignore[reportPrivateUsage]
                             second.started_at)
        self.assertEqual(calls, 2)

    def test_mac_file_growth_commits_actual_received_cursor(self) -> None:
        content = b'{"cmd":"hana_shot.py shot"}\n'

        def fake_ssh(args: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
            output = cast(BinaryIO, options["stdout"])
            rows: list[dict[str, object]] = [
                {"kind": "claude_coverage", "included": False},
                {"kind": "file", "key": "codex/growing.jsonl", "size": len(content) - 4,
                 "inode": 3, "mtime": 1, "matched": True, "start": 0},
                _chunk_record("codex/growing.jsonl", content),
                {"kind": "end", "finished": True},
            ]
            for row in rows:
                _ = output.write((json.dumps(row) + "\n").encode())
            return subprocess.CompletedProcess(args, 0, stderr=b"rc=0\n")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch.object(subprocess, "run", side_effect=fake_ssh):
                result = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
            assert isinstance(result, report_module.MacEvidenceAvailable)
            self.assertEqual((state_dir / "mac-source/codex/growing.jsonl").read_bytes(), content)
            manifest = report_module._read_object(state_dir / "mac_manifest.json")  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(cast(dict[str, object], manifest["codex/growing.jsonl"])["cursor"], len(content))

    def test_mac_ssh_timeout_commits_no_partial_transfer(self) -> None:
        def fake_ssh(args: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
            output = cast(BinaryIO, options["stdout"])
            _ = output.write(b'{"kind":"file","key":"codex/partial.jsonl","size":20,' +
                             b'"inode":3,"mtime":1,"matched":true,"start":0}\n')
            raise subprocess.TimeoutExpired(args, report_module.MAC_SSH_TIMEOUT_SECONDS)

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with mock.patch.object(subprocess, "run", side_effect=fake_ssh):
                result = report_module._mac_evidence(state_dir)  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(result, report_module.MacEvidenceUnavailable)
            self.assertFalse((state_dir / "mac_manifest.json").exists())
            self.assertFalse((state_dir / "mac-source").exists())

    def test_host_coverage_uses_read_start_not_scan_completion(self) -> None:
        local_start = datetime.fromisoformat("2026-10-06T20:00:00+00:00")
        mac_start = local_start + timedelta(minutes=1)
        completed = local_start + timedelta(minutes=5)
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            mirror = state_dir / "mac-source"
            mac = report_module.MacEvidenceAvailable(mirror / "claude", mirror / "codex",
                                                     mirror / "timings/timings.jsonl", 0, False,
                                                     mac_start, report_module.MacReadFinished())
            with (mock.patch.object(report_module, "_utc_now", side_effect=[local_start, completed]),
                  mock.patch.object(report_module, "_mac_evidence", return_value=mac)):
                _ = report_module.scan(state_dir, state_dir / "claude", state_dir / "codex", include_mac=True)
            coverage = report_module._host_coverage(state_dir)  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(coverage["natedev"].at, local_start)
            self.assertEqual(coverage["mac"].at, mac_start)
            self.assertEqual(report_module._read_object(state_dir / "scan_status.json")["last_success"],  # pyright: ignore[reportPrivateUsage]
                             completed.isoformat())

    def test_legacy_mac_coverage_excludes_claude_even_when_old_status_says_included(self) -> None:
        baseline = next(episode for episode in read_episodes(self.state_dir / "episodes.jsonl") if episode.split == 300)
        change_time = datetime.fromisoformat("2026-10-01T00:30:00+00:00")
        covered_at = change_time + timedelta(hours=1)
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            report_module._save_object(state_dir / "scan_status.json", {  # pyright: ignore[reportPrivateUsage]
                "host_last_success": {"mac": covered_at.isoformat(), "natedev": covered_at.isoformat()},
                "mac_claude": "included",
            })
            write_episodes(state_dir / "episodes.jsonl", [
                replace(baseline, source_host="mac", method="by hand",
                        start=change_time - timedelta(minutes=10), end=change_time - timedelta(minutes=9)),
                replace(baseline, source_host="mac", method="/hana_shot",
                        start=change_time + timedelta(minutes=10), end=change_time + timedelta(minutes=11)),
            ])
            write_changes(state_dir / "changes.json", [
                ProductChange("claude", "legacy-mac", "Mac crop", change_time, ("mac",)),
            ])
            output = report_module.report(state_dir)
        mac_coverage = next(line for line in output.splitlines()
                            if line.startswith("mac evidence covered through "))
        local_coverage = next(line for line in output.splitlines()
                              if line.startswith("natedev evidence covered through "))
        self.assertIn("sources: Codex transcripts, timings", mac_coverage)
        self.assertNotIn("Claude transcripts", mac_coverage)
        self.assertIn("sources: Claude transcripts, Codex transcripts, timings", local_coverage)
        mac_rows = [line for line in output.splitlines() if "legacy-mac" in line and "host=mac" in line]
        self.assertTrue(mac_rows)
        self.assertTrue(all("incomplete:mac" in line for line in mac_rows), mac_rows)

    def test_local_coverage_names_timings_only_when_timing_source_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            claude_root = FIXTURES / "claude" / "projects"
            codex_root = FIXTURES / "codex" / "sessions"
            _ = report_module.scan(state_dir, claude_root, codex_root)
            without = report_module._read_object(state_dir / "scan_status.json")  # pyright: ignore[reportPrivateUsage]
            without_hosts = cast(dict[str, object], without["host_coverage"])
            without_local = cast(dict[str, object], without_hosts["natedev"])
            self.assertEqual(set(cast(list[str], without_local["sources"])), {"claude", "codex"})

            timings = state_dir / "timings.jsonl"
            _ = timings.write_text("", encoding="utf-8")
            _ = report_module.scan(state_dir, claude_root, codex_root,
                                   AvailableTimingSource(timings, "natedev"))
            with_timing = report_module._read_object(state_dir / "scan_status.json")  # pyright: ignore[reportPrivateUsage]
            with_hosts = cast(dict[str, object], with_timing["host_coverage"])
            local = cast(dict[str, object], with_hosts["natedev"])
            self.assertEqual(set(cast(list[str], local["sources"])), {"claude", "codex", "timings"})

    def test_mac_finished_without_claude_transcripts_never_labels_window_complete(self) -> None:
        baseline = next(episode for episode in read_episodes(self.state_dir / "episodes.jsonl") if episode.split == 300)
        change_time = datetime.fromisoformat("2026-10-01T00:30:00+00:00")
        mac_start = change_time + timedelta(hours=2)
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            mirror = state_dir / "mac-source"
            mac = report_module.MacEvidenceAvailable(mirror / "claude-unavailable", mirror / "codex",
                                                     mirror / "timings/timings.jsonl", 0, False,
                                                     mac_start, report_module.MacReadFinished())
            with mock.patch.object(report_module, "_mac_evidence", return_value=mac):
                _ = report_module.scan(state_dir, state_dir / "local-claude", state_dir / "local-codex",
                                       include_mac=True)
            covered = report_module._host_coverage(state_dir)["mac"]  # pyright: ignore[reportPrivateUsage]
            self.assertIsInstance(covered, report_module.CoveredHostSourcesThrough)
            self.assertEqual(covered.sources, frozenset(("codex", "timings")))
            write_episodes(state_dir / "episodes.jsonl", [
                replace(baseline, source_host="mac", method="by hand",
                        start=change_time - timedelta(minutes=10), end=change_time - timedelta(minutes=9)),
                replace(baseline, source_host="mac", method="/hana_shot",
                        start=change_time + timedelta(minutes=10), end=change_time + timedelta(minutes=11)),
            ])
            write_changes(state_dir / "changes.json", [
                ProductChange("claude", "mac-crop", "Crop on Mac", change_time, ("mac",)),
            ])
            output = report_module.report(state_dir)
        mac_rows = [line for line in output.splitlines() if "mac-crop" in line and "host=mac" in line]
        self.assertTrue(mac_rows)
        self.assertTrue(all("incomplete:mac" in line for line in mac_rows), mac_rows)
        self.assertIn("Mac Claude transcripts: out", output)
        self.assertIn("Codex transcripts", output)
        self.assertIn("timings", output)

    def test_report_shows_saved_mac_read_state_beside_last_coverage(self) -> None:
        mac_start = datetime.fromisoformat("2026-10-06T20:00:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            mirror = state_dir / "mac-source"
            states: list[tuple[str, report_module.MacEvidence]] = [
                ("finished", report_module.MacEvidenceAvailable(
                    mirror / "claude-unavailable", mirror / "codex", mirror / "timings/timings.jsonl",
                    0, False, mac_start, report_module.MacReadFinished())),
                ("catching up", report_module.MacEvidenceAvailable(
                    mirror / "claude-unavailable", mirror / "codex", mirror / "timings/timings.jsonl",
                    0, False, mac_start + timedelta(minutes=1), report_module.MacReadCatchingUp())),
                ("unavailable", report_module.MacEvidenceUnavailable("asleep")),
            ]
            for expected, evidence in states:
                with self.subTest(state=expected), mock.patch.object(report_module, "_mac_evidence", return_value=evidence):
                    _ = report_module.scan(state_dir, state_dir / "local-claude",
                                           state_dir / "local-codex", include_mac=True)
                    status = report_module._read_object(state_dir / "scan_status.json")  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(str(status["mac_read_state"]).replace("_", " "), expected)
                    mac_line = next(line for line in report_module.report(state_dir).splitlines()
                                    if line.startswith("Mac:"))
                    self.assertIn(expected, mac_line)
                    self.assertIn(report_module._pacific(mac_start), mac_line)  # pyright: ignore[reportPrivateUsage]

    def test_change_with_missing_required_key_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changes.json"
            _ = path.write_text(json.dumps([{"repository": "claude", "commit": "abc", "summary": "text",
                                             "host_coverage": ["natedev"]}]))
            with self.assertRaisesRegex(ValueError, "invalid change"):
                _ = read_changes(path)


if __name__ == "__main__":
    _ = unittest.main()
