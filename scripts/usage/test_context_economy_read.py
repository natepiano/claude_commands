"""Read context events and settings from isolated transcript, Git, and ledger fixtures."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import cast, final, override
from unittest.mock import patch

from scripts.usage import context_economy_read as reader


FIXTURES = Path(__file__).with_name("fixtures") / "context_economy"


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@final
class ContextEconomyReadTests(unittest.TestCase):
    since: datetime = instant("2026-11-01T10:00:00Z")
    until: datetime = instant("2026-11-01T11:00:00Z")

    @cached_property
    def root(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    @property
    def projects(self) -> Path:
        return self.root / "home/.claude/projects"

    @property
    def checkout(self) -> Path:
        return self.root / "checkout"

    @override
    def setUp(self) -> None:
        self.projects.mkdir(parents=True)
        self.checkout.mkdir()
        configuration = self.root / "configuration"
        configuration.mkdir()
        _ = (configuration / "context_economy.conf").write_text(
            "settings_keys = autoCompactWindow precomputeCompactionEnabled model fixtureTracked absentFixture\n"
            + "files = commands/unit/direct.md docs/direct/write_prompt_contract.md docs/fixture-policy.md\n"
            + "min_requests = 20\n"
            + "active_gap_seconds = 600\n", encoding="utf-8")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "CONF_DIR": str(configuration),
            "CONTEXT_ECONOMY_CHECKOUT": str(self.checkout),
            "USAGE_STATE_DIR": str(self.root / "state"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })))

    def transcript(self, fixture: str, relative: str) -> Path:
        target = self.projects / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(FIXTURES / fixture, target)
        stamp = (self.until + timedelta(seconds=1)).timestamp()
        os.utime(target, (stamp, stamp))
        return target

    def parent_session(self) -> reader.Session:
        _ = self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl")
        session, = reader.read_claude(self.projects, self.since, self.until, "fixture-machine")
        return session

    def git(self, *arguments: str, at: str = "2026-11-01T09:00:00Z") -> str:
        environment = dict(os.environ, GIT_AUTHOR_DATE=at, GIT_COMMITTER_DATE=at)
        result = subprocess.run([
            "git", "-C", str(self.checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *arguments,
        ], env=environment, check=True, capture_output=True, text=True, timeout=15)
        return result.stdout.strip()

    def commit(self, message: str, at: str) -> str:
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", message, at=at)
        return self.git("rev-parse", "HEAD")

    def settings_history(self) -> tuple[list[str], bytes, bytes]:
        _ = self.git("init", "--initial-branch=main")
        settings: dict[str, object] = {
            "autoCompactWindow": 12345, "precomputeCompactionEnabled": True,
            "model": "fixture-model", "fixtureTracked": "tracked from configuration", "ignored": "before",
        }
        settings_file = self.checkout / "settings.json"
        _ = settings_file.write_text(json.dumps(settings), encoding="utf-8")
        first_content = b"First direct instructions.\n"
        last_content = b"Changed direct instructions with more text.\n"
        for name, content in (
            ("commands/unit/direct.md", first_content),
            ("docs/direct/write_prompt_contract.md", b"Prompt contract.\n"),
            ("docs/fixture-policy.md", b"A tracked fixture policy.\n"),
        ):
            path = self.checkout / name
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_bytes(content)
        baseline = self.commit("Record the initial fixture settings", "2026-11-01T09:00:00Z")
        settings["precomputeCompactionEnabled"] = False
        _ = settings_file.write_text(json.dumps(settings), encoding="utf-8")
        changed_setting = self.commit("Disable precomputed compaction", "2026-11-01T10:10:00Z")
        settings["ignored"] = "after"
        _ = settings_file.write_text(json.dumps(settings), encoding="utf-8")
        _ = self.commit("Change an untracked setting", "2026-11-01T10:20:00Z")
        _ = (self.checkout / "commands/unit/direct.md").write_bytes(last_content)
        changed_file = self.commit("Change the direct instructions", "2026-11-01T10:30:00Z")
        return [baseline, changed_setting, changed_file], first_content, last_content

    def test_requests_use_the_last_usage_for_each_message_and_request_pair(self) -> None:
        session = self.parent_session()
        actual = sorted((request.at, request.model, request.cli_version, request.input, request.cache_read,
                         request.cache_write_5m, request.cache_write_1h, request.output, request.context)
                        for request in session.requests)
        self.assertEqual(actual, [
            (instant("2026-11-01T10:02:00Z"), "fixture-model", "2.1.2", 40, 1000, 12, 30, 7, 1082),
            (instant("2026-11-01T10:03:00Z"), "fixture-other-model", "2.1.2", 5, 200, 9, 0, 3, 214),
            (instant("2026-11-01T10:04:00Z"), "fixture-model", "2.1.2", 8, 0, 0, 0, 1, 8),
        ])

    def test_compactions_keep_the_kind_trigger_and_token_counts(self) -> None:
        session = self.parent_session()
        self.assertEqual([(event.at, event.kind, event.trigger, event.pre_tokens, event.post_tokens)
                          for event in session.compactions], [
            (instant("2026-11-01T10:10:00Z"), "precomputed", "auto", 9000, 400),
            (instant("2026-11-01T10:25:00Z"), "normal", "manual", 8000, 600),
            (instant("2026-11-01T10:35:00Z"), "unknown", None, None, None),
            (instant("2026-11-01T10:45:00Z"), "normal", "auto", 7500, 500),
            (instant("2026-11-01T10:50:00Z"), "normal", None, None, None),
        ])

    def test_startup_and_resume_hooks_give_one_start_at_the_first_hook_time(self) -> None:
        expected = [
            (self.since, "startup", "2.1.1"),
            (instant("2026-11-01T10:00:01Z"), "resume", "2.1.2"),
            (instant("2026-11-01T10:01:00Z"), "resume", "2.1.1"),
            (instant("2026-11-01T10:02:00Z"), "resume", "2.1.2"),
            (instant("2026-11-01T10:20:00Z"), "resume", "2.1.3"),
            (instant("2026-11-01T10:20:01Z"), "resume", "2.1.4"),
            (instant("2026-11-01T10:22:00Z"), "resume", "2.1.5"),
            (instant("2026-11-01T10:30:00Z"), "resume", "2.1.4"),
        ]
        for fixture, since, starts in (
            ("parent-fixture.jsonl", self.since, expected),
            ("parent-fixture.jsonl", self.since + timedelta(seconds=1), expected),
            ("parent-fixture.jsonl", instant("2026-11-01T10:20:01Z"), expected[4:]),
            ("parent-fixture.jsonl", instant("2026-11-01T10:21:00Z"), expected[5:]),
            ("version-restarts.jsonl", self.since, [
                (instant("2026-11-01T09:50:00Z"), "startup", "2.1.296"),
                (instant("2026-11-01T10:01:00Z"), "resume", "2.1.316"),
                (instant("2026-11-01T10:03:00Z"), "resume", "2.1.317"),
                (instant("2026-11-01T10:04:00Z"), "resume", "2.1.318"),
                (instant("2026-11-01T10:05:00Z"), "resume", "2.1.319"),
            ]),
        ):
            with self.subTest(fixture=fixture, since=since):
                _ = self.transcript(fixture, "project/starts.jsonl")
                session, = reader.read_claude(self.projects, since, self.until, "fixture-machine")
                self.assertEqual([(start.at, start.source, start.cli_version) for start in session.starts], starts)

    def test_window_bounds_and_file_modification_time_select_the_requests(self) -> None:
        path = self.transcript("window.jsonl", "project/window.jsonl")
        for modified, expected in (
            (self.since - timedelta(seconds=1), []),
            (self.since, ["since", "inside"]),
            (self.until, ["since", "inside"]),
        ):
            with self.subTest(modified=modified):
                stamp = modified.timestamp()
                os.utime(path, (stamp, stamp))
                sessions = reader.read_claude(self.projects, self.since, self.until, "fixture-machine")
                self.assertEqual([request.model for session in sessions for request in session.requests], expected)
                expected_times = [self.since] if expected else []
                self.assertEqual([event.at for session in sessions for event in session.compactions], expected_times)
                start_times = [instant("2026-11-01T09:59:59Z"), self.since] if expected else []
                self.assertEqual([start.at for session in sessions for start in session.starts], start_times)
                first_times = [instant("2026-11-01T09:59:59Z")] if expected else []
                self.assertEqual([session.first_record_at for session in sessions], first_times)

    def test_subagents_keep_the_parent_session_and_their_own_requests(self) -> None:
        _ = self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl")
        _ = self.transcript("agent-child-fixture.jsonl", "project/parent-fixture/subagents/agent-child-fixture.jsonl")
        sessions = reader.read_claude(self.projects, self.since, self.until, "fixture-machine")
        self.assertEqual(sorted((session.machine, session.family, session.session_id, session.agent_id,
                                 [request.context for request in session.requests]) for session in sessions), [
            ("fixture-machine", "claude", "parent-fixture", "child-fixture", [331]),
            ("fixture-machine", "claude", "parent-fixture", "parent-fixture", [1082, 214, 8]),
        ])

    def test_settings_timeline_keeps_the_baseline_and_only_tracked_changes(self) -> None:
        commits, first_content, last_content = self.settings_history()
        tree = self.git("rev-parse", "HEAD^{tree}")
        merge = self.git("commit-tree", tree, "-p", commits[0], "-p", commits[-1],
                         "-m", "Merge the settings branch", at="2026-11-01T12:00:00Z")
        _ = self.git("merge", "--ff-only", merge, at="2026-11-01T10:40:00Z")
        timeline = reader.settings_timeline(self.checkout, self.since, self.until)
        self.assertEqual([entry.commit for entry in timeline], commits)
        expected_values: dict[str, object] = {
            "autoCompactWindow": 12345, "precomputeCompactionEnabled": True, "model": "fixture-model",
            "fixtureTracked": "tracked from configuration", "absentFixture": None,
            "commands/unit/direct.md": {"bytes": len(first_content), "sha": hashlib.sha256(first_content).hexdigest()[:12]},
            "docs/direct/write_prompt_contract.md": {"bytes": 17, "sha": hashlib.sha256(b"Prompt contract.\n").hexdigest()[:12]},
            "docs/fixture-policy.md": {"bytes": 26, "sha": hashlib.sha256(b"A tracked fixture policy.\n").hexdigest()[:12]},
        }
        self.assertEqual(timeline[0].values, expected_values)
        expected_values["precomputeCompactionEnabled"] = False
        self.assertEqual(timeline[1].values, expected_values)
        expected_values["commands/unit/direct.md"] = {
            "bytes": len(last_content), "sha": hashlib.sha256(last_content).hexdigest()[:12],
        }
        self.assertEqual(timeline[2].values, expected_values)
        self.assertEqual([entry.at for entry in timeline[1:]],
                         [instant("2026-11-01T10:10:00Z"), instant("2026-11-01T10:30:00Z")])
        for since, until, expected in (
            (self.since, instant("2026-11-01T10:30:00Z"), commits[:2]),
            (instant("2026-11-01T10:15:00Z"), self.until, commits[1:]),
            (instant("2026-11-01T10:40:00Z"), self.until, commits[2:]),
        ):
            with self.subTest(since=since, until=until):
                window = reader.settings_timeline(self.checkout, since, until)
                self.assertEqual([entry.commit for entry in window], expected)

    def test_settings_for_uses_the_latest_set_at_or_before_the_start(self) -> None:
        _ = self.settings_history()
        timeline = reader.settings_timeline(self.checkout, self.since, self.until)
        later = reader.SettingsSet(timeline[2].at, "later-commit", {**timeline[2].values, "model": "later-model"})
        for start, choices, index in (
            (instant("2026-11-01T10:00:00Z"), timeline, 0),
            (instant("2026-11-01T10:10:00Z"), timeline, 1),
            (instant("2026-11-01T10:25:00Z"), timeline, 1),
            (instant("2026-11-01T10:30:00Z"), timeline, 2),
            (timeline[2].at, [*timeline, later], 3),
        ):
            with self.subTest(start=start):
                self.assertEqual(reader.settings_for(start, choices), choices[index])
        self.assertIsInstance(reader.settings_for(timeline[0].at - timedelta(seconds=1), timeline), reader.SettingsUnknown)
        self.assertIsInstance(reader.settings_for(self.since, []), reader.SettingsUnknown)

    def test_label_names_the_changed_setting_or_file(self) -> None:
        _ = self.settings_history()
        timeline = reader.settings_timeline(self.checkout, self.since, self.until)
        self.assertEqual(reader.label(timeline[1], timeline[0]), "precomputeCompactionEnabled=false")
        file_label = reader.label(timeline[2], timeline[1])
        self.assertIn("commands/unit/direct.md", file_label)
        self.assertNotIn("precomputeCompactionEnabled", file_label)
        self.assertNotIn("ignored", file_label)

    def test_role_of_uses_the_latest_matching_ledger_row_or_unknown(self) -> None:
        store = self.root / "usage.sqlite3"
        with closing(sqlite3.connect(store)) as db:
            _ = db.execute("CREATE TABLE turns (machine TEXT, family TEXT, parent_id TEXT, agent_id TEXT, at TEXT, role TEXT)")
            _ = db.executemany("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?)", [
                ("fixture-machine", "claude", "parent-fixture", "child-fixture", "2026-11-01T10:30:00Z", "review"),
                ("fixture-machine", "claude", "parent-fixture", "child-fixture", "2026-11-01T10:10:00Z", "impl"),
                ("other-machine", "claude", "parent-fixture", "child-fixture", "2026-11-01T10:50:00Z", "wrong-machine"),
                ("fixture-machine", "codex", "parent-fixture", "child-fixture", "2026-11-01T10:50:00Z", "wrong-family"),
                ("fixture-machine", "claude", "other-parent", "child-fixture", "2026-11-01T10:50:00Z", "wrong-parent"),
                ("fixture-machine", "claude", "parent-fixture", "other-child", "2026-11-01T10:50:00Z", "wrong-agent"),
                ("fixture-machine", "claude", "parent-fixture", "child-fixture", "2026-11-01T10:30:00Z", "fix"),
                ("fixture-machine", "claude", "", "parent-fixture", "2026-11-01T10:40:00Z", "showrunner"),
                ("fixture-machine", "claude", "", "parent-fixture", "2026-11-01T10:05:00Z", "unit"),
            ])
            db.commit()
        before = store.read_bytes()
        for path, agent, expected in (
            (store, "child-fixture", "fix"),
            (store, "parent-fixture", "showrunner"),
            (store, "missing-child", "unknown"),
            (self.root / "missing.sqlite3", "child-fixture", "unknown"),
        ):
            with self.subTest(store=path.name, agent=agent):
                self.assertEqual(reader.role_of(path, "fixture-machine", "claude", "parent-fixture", agent), expected)
        self.assertEqual(store.read_bytes(), before)


if __name__ == "__main__":
    _ = unittest.main()
