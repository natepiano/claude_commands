"""Contract tests for screenshot episodes and the command output."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, TypedDict, cast, override

from scripts.shot_report.episodes import read_episodes, write_episodes


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


HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
COMMAND = HERE / "shot_report.py"


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
        return subprocess.run(
            [sys.executable, str(COMMAND), *arguments, "--state-dir", str(cls.state_dir)],
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
        target = self.state_dir / "legacy.jsonl"
        _ = target.write_text(json.dumps(old) + "\n", encoding="utf-8")
        loaded = read_episodes(target)
        self.assertEqual(len(loaded), 1)
        self.assertIsInstance(loaded[0].source, frozenset)
        self.assertEqual(loaded[0].image_count, loaded[0].screenshot_count)

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

    def test_survey_shows_call_session_and_project_counts(self) -> None:
        result = self.run_command("survey")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("source calls sessions projects counted_as_by_hand", result.stdout)
        self.assertIn("mcp_brp 4 2 1 yes", result.stdout)
        self.assertIn("bash_brp 1 1 1 yes", result.stdout)
        self.assertIn("hana_shot 3 3 1 yes", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
