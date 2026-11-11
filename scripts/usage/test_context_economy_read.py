"""Read context events and settings from isolated transcript, Git, and ledger fixtures."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from functools import cached_property
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import BinaryIO, cast, final, override
from unittest.mock import Mock, call, patch

from scripts.lib import conf
from scripts.usage import context_economy_read as reader
from scripts.usage import context_economy_cache as cache_module


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
            + "active_gap_seconds = 600\n"
            + "# whole: 1..\nmac_timeout_seconds = 300\n"
            + "# whole: 1..\ncache_max_megabytes = 512\n# whole: 1..\nprogress_seconds = 1\n", encoding="utf-8")
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
            (instant("2026-11-01T10:35:00Z"), "unknown", None, reader.SizeNotReported(), reader.SizeNotReported()),
            (instant("2026-11-01T10:45:00Z"), "normal", "auto", 7500, 500),
            (instant("2026-11-01T10:50:00Z"), "normal", None, reader.SizeNotReported(), reader.SizeNotReported()),
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
            "fixtureTracked": "tracked from configuration",
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

        path = self.checkout / "settings.json"
        values = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        del values["fixtureTracked"]
        values["absentFixture"] = None
        _ = path.write_text(json.dumps(values), encoding="utf-8")
        removed = self.commit("Remove a tracked setting and add an explicit null", "2026-11-01T10:50:00Z")
        timeline = reader.settings_timeline(self.checkout, self.since, self.until)
        self.assertEqual([entry.commit for entry in timeline], [*commits, removed])
        del expected_values["fixtureTracked"]
        expected_values["absentFixture"] = None
        self.assertEqual(timeline[-1].values, expected_values)
        self.assertEqual(reader.label(timeline[-1], timeline[-2]), "absentFixture=null, fixtureTracked=<absent>")

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
        cases: tuple[tuple[dict[str, object], dict[str, object], str], ...] = (
            ({"fixtureTracked": "present"}, {}, "fixtureTracked=<absent>"),
            ({}, {"fixtureTracked": None}, "fixtureTracked=null"),
            ({"fixtureTracked": None}, {}, "fixtureTracked=<absent>"),
        )
        for before, after, expected in cases:
            with self.subTest(before=before, after=after):
                previous = reader.SettingsSet(self.since, "before", before)
                current = reader.SettingsSet(self.until, "after", after)
                self.assertEqual(reader.label(current, previous), expected)

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

    def codex_rollout(self) -> Path:
        path = self.root / "home/.codex/sessions/2026/11/01/rollout-fixture.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(FIXTURES / "codex-fixture.jsonl", path)
        stamp = (self.until + timedelta(seconds=1)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def test_codex_requests_keep_the_model_version_tokens_and_compaction_evidence(self) -> None:
        path = self.codex_rollout()
        session, = reader.read_codex([path.parents[3]], self.since, self.until, "fixture-machine")
        self.assertEqual((session.machine, session.family, session.session_id, session.agent_id),
                         ("fixture-machine", "codex", "codex-fixture", "codex-fixture"))
        self.assertEqual([(item.at, item.model, item.cli_version, item.input, item.cache_read,
                           item.cache_write_5m, item.cache_write_1h, item.output, item.context)
                          for item in session.requests], [
            (self.since, "gpt-6.1-sol", "0.101.0", 200, 800, 0, 0, 100, 1000),
            (instant("2026-11-01T10:11:00Z"), "gpt-6-astra", "0.101.0", 200, 100, 0, 0, 50, 300),
            (instant("2026-11-01T10:21:00Z"), "gpt-6-astra", "0.102.0", 300, 50, 0, 0, 5, 350),
        ])
        event, = session.compactions
        self.assertEqual((event.at, event.kind, event.trigger), (instant("2026-11-01T10:05:00Z"), "unknown", None))
        self.assertIsInstance(event.pre_tokens, reader.SizeNotReported)
        self.assertIsInstance(event.post_tokens, reader.SizeNotReported)
        self.assertEqual(session.first_record_at, instant("2026-11-01T09:20:00Z"))
        later, = reader.read_codex([path.parents[3]], self.since + timedelta(seconds=15), self.until, "fixture-machine")
        self.assertEqual([item.at for item in later.requests],
                         [instant("2026-11-01T10:11:00Z"), instant("2026-11-01T10:21:00Z")])

    def test_codex_starts_keep_one_earlier_anchor_and_the_starts_inside_the_window(self) -> None:
        path = self.codex_rollout()
        for since, expected in (
            (self.since, [("2026-11-01T09:45:00Z", "resume", "0.101.0"),
                          ("2026-11-01T10:20:00Z", "resume", "0.102.0")]),
            (instant("2026-11-01T09:20:00Z"), [("2026-11-01T09:20:00Z", "startup", "0.100.0"),
                                               ("2026-11-01T09:45:00Z", "resume", "0.101.0"),
                                               ("2026-11-01T10:20:00Z", "resume", "0.102.0")]),
            (instant("2026-11-01T10:20:01Z"), [("2026-11-01T10:20:00Z", "resume", "0.102.0")]),
        ):
            with self.subTest(since=since):
                session, = reader.read_codex([path.parents[3]], since, self.until, "fixture-machine")
                self.assertEqual([(start.at, start.source, start.cli_version) for start in session.starts],
                                 [(instant(at), source, version) for at, source, version in expected])

    def test_codex_linked_homes_and_overlapping_roots_read_each_rollout_once(self) -> None:
        path = self.codex_rollout()
        shared = path.parents[3]
        numbered = self.root / "home/.codex1/sessions"
        numbered.parent.mkdir()
        numbered.symlink_to(shared, target_is_directory=True)
        settings = conf.read("context_economy")
        with (patch.object(conf, "read", return_value=settings),
              patch.object(reader, "_read_codex_file", wraps=getattr(reader, "_read_codex_file")) as opened):
            sessions = reader.read_codex([shared, numbered, shared / "2026"], self.since, self.until, "fixture-machine")
            opened.assert_called_once()
        session, = sessions
        self.assertEqual([item.context for item in session.requests], [1000, 300, 350])

    def cached_families(self, since: datetime) -> list[reader.Session]:
        return [*reader.read_claude(self.projects, since, self.until, "fixture-machine"),
                *reader.read_codex([self.root / "home/.codex/sessions"], since, self.until, "fixture-machine")]

    def clear_summary_cache(self) -> None:
        shutil.rmtree(self.root / "home/.cache/context_economy")

    def test_warm_cache_parses_no_transcript_and_keeps_the_same_sessions(self) -> None:
        _ = self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl")
        _ = self.codex_rollout()
        cold = self.cached_families(self.since)
        self.assertEqual({session.family for session in cold}, {"claude", "codex"})
        with (patch.object(reader, "_read_transcript", side_effect=AssertionError("warm Claude parse")),
              patch.object(reader, "_read_codex_file", side_effect=AssertionError("warm Codex parse")),
              patch.object(cache_module, "prefix_hash", side_effect=AssertionError("warm prefix read"))):
            self.assertEqual(self.cached_families(self.since), cold)

    def test_appends_resume_both_parsers_at_the_last_complete_line(self) -> None:
        paths = [self.transcript("version-restarts.jsonl", "project/versions.jsonl"), self.codex_rollout()]
        original = {path: path.read_bytes() for path in paths}
        offsets: list[int] = []
        for path, content in original.items():
            lines = content.splitlines(keepends=True)
            middle = len(lines) // 2
            prefix = b"".join(lines[:middle])
            offsets.append(len(prefix))
            _ = path.write_bytes(prefix + lines[middle][:len(lines[middle]) // 2])
        _ = self.cached_families(self.since)
        for path, content in original.items():
            with path.open("ab") as stream:
                _ = stream.write(content[path.stat().st_size:])
        positions: list[int] = []
        complete_lines = reader.complete_lines

        def resumed_lines(stream: BinaryIO) -> Iterator[tuple[int, bytes]]:
            positions.append(stream.tell())
            yield from complete_lines(stream)

        with patch.object(reader, "complete_lines", side_effect=resumed_lines):
            resumed = self.cached_families(self.since)
        self.assertEqual(positions, offsets)
        self.clear_summary_cache()
        self.assertEqual(self.cached_families(self.since), resumed)

    def test_same_size_rewrite_and_truncation_restart_the_parser(self) -> None:
        path = self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl")
        original = path.read_bytes()
        for content in (original.replace(b"fixture-model", b"another-model"), original.splitlines(keepends=True)[0]):
            with self.subTest(size=len(content)):
                _ = self.cached_families(self.since)
                _ = path.write_bytes(content)
                with patch.object(reader, "_read_transcript", wraps=getattr(reader, "_read_transcript")) as parse:
                    changed = self.cached_families(self.since)
                parse.assert_called_once_with(path.resolve(), cache_module.ParseCheckpoint())
                self.clear_summary_cache()
                self.assertEqual(self.cached_families(self.since), changed)

    def test_rewrite_then_append_matches_a_cold_parse_for_both_families(self) -> None:
        paths = [self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl"), self.codex_rollout()]
        cold = self.cached_families(self.since)
        for path, old, new in zip(paths, (b"fixture-model", b"gpt-6.1-sol"),
                                  (b"another-model", b"gpt-6.2-sol")):
            original = path.read_bytes()
            changed = original.replace(old, new)
            self.assertEqual(len(changed), len(original))
            self.assertNotEqual(changed, original)
            _ = path.write_bytes(changed + b"{}\n")
        updated = self.cached_families(self.since)
        self.assertNotEqual(updated, cold)
        self.clear_summary_cache()
        self.assertEqual(self.cached_families(self.since), updated)

    def test_prefix_hash_covers_all_blocks_and_stops_at_the_saved_offset(self) -> None:
        path = self.projects / "blocks.jsonl"
        prefix = b"a" * (2 * 1024 * 1024 + 17)
        _ = path.write_bytes(prefix + b"unparsed tail")
        digest = cache_module.prefix_hash(path, len(prefix))
        self.assertEqual(digest, hashlib.blake2b(prefix).hexdigest())
        _ = path.write_bytes(prefix + b"different tail")
        self.assertEqual(cache_module.prefix_hash(path, len(prefix)), digest)
        middle = len(prefix) // 2
        _ = path.write_bytes(prefix[:middle] + b"b" + prefix[middle + 1:] + b"different tail")
        self.assertNotEqual(cache_module.prefix_hash(path, len(prefix)), digest)

    def test_middle_request_rewrite_then_growth_matches_a_cold_parse(self) -> None:
        path = self.transcript("parent-fixture.jsonl", "project/middle.jsonl")
        request = next(line for line in path.read_bytes().splitlines(keepends=True)
                       if b'"requestId":"request-b"' in line)
        padding = b"{}\n" * 1700
        original = padding + request + padding
        marker = b'"input_tokens":5'
        position = original.index(marker)
        self.assertGreater(position, 4096)
        self.assertLess(position + len(marker), len(original) - 4096)
        _ = path.write_bytes(original)
        cold = self.cached_families(self.since)
        changed = original.replace(marker, b'"input_tokens":9')
        self.assertEqual(len(changed), len(original))
        _ = path.write_bytes(changed + b"{}\n")
        with patch.object(reader, "_read_transcript", wraps=getattr(reader, "_read_transcript")) as parse:
            updated = self.cached_families(self.since)
        parse.assert_called_once_with(path.resolve(), cache_module.ParseCheckpoint())
        self.assertNotEqual(updated, cold)
        self.clear_summary_cache()
        self.assertEqual(self.cached_families(self.since), updated)

    def test_resume_checks_both_ends_of_the_saved_prefix(self) -> None:
        path = self.projects / "samples.jsonl"
        original = b"a" * 16383 + b"\n"
        for position in (0, len(original) - 2):
            with self.subTest(position=position), closing(cache_module.TranscriptCache()) as cache:
                _ = path.write_bytes(original)
                checkpoint = cache_module.ParseCheckpoint(len(original), "saved")
                self.assertEqual(cache.read(path, "claude", lambda path, held: checkpoint), checkpoint)
                _ = path.write_bytes(original[:position] + b"b" + original[position + 1:] + b"next\n")
                parse = Mock(return_value=cache_module.ParseCheckpoint(path.stat().st_size, "rebuilt"))
                parse_file = cast(Callable[[Path, cache_module.ParseCheckpoint], cache_module.ParseCheckpoint], parse)
                self.assertEqual(cache.read(path, "claude", parse_file),
                                 cache_module.ParseCheckpoint(path.stat().st_size, "rebuilt"))
                parse.assert_called_once_with(path.resolve(), cache_module.ParseCheckpoint())

    def test_warm_access_releases_the_transaction_before_another_parse(self) -> None:
        path = self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl")
        other = self.projects / "other.jsonl"
        _ = other.write_bytes(b"{}\n")
        checkpoint = cache_module.ParseCheckpoint(path.stat().st_size, "saved")
        with closing(cache_module.TranscriptCache()) as first, closing(cache_module.TranscriptCache()) as second:
            _ = second.database.execute("PRAGMA busy_timeout = 0")
            self.assertEqual(first.read(path, "claude", lambda path, held: checkpoint), checkpoint)
            self.assertEqual(first.read(path, "claude", lambda path, held: checkpoint), checkpoint)
            self.assertFalse(first.database.in_transaction)

            def parse_other(source: Path, held: cache_module.ParseCheckpoint) -> cache_module.ParseCheckpoint:
                self.assertEqual(held, cache_module.ParseCheckpoint())
                self.assertFalse(first.database.in_transaction)
                second.mark_final(path)
                self.assertFalse(second.database.in_transaction)
                return cache_module.ParseCheckpoint(source.stat().st_size, "other")

            self.assertEqual(first.read(other, "claude", parse_other), cache_module.ParseCheckpoint(3, "other"))
            self.assertFalse(first.database.in_transaction)

    def test_another_window_reuses_all_cached_events(self) -> None:
        _ = self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl")
        _ = self.codex_rollout()
        _ = self.cached_families(instant("2026-11-01T10:30:00Z"))
        with (patch.object(reader, "_read_transcript", side_effect=AssertionError("window reparsed Claude")),
              patch.object(reader, "_read_codex_file", side_effect=AssertionError("window reparsed Codex"))):
            broader = self.cached_families(self.since)
        self.clear_summary_cache()
        self.assertEqual(self.cached_families(self.since), broader)

    def test_deleted_transcripts_keep_their_cached_sessions_and_last_mtime(self) -> None:
        paths = [self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl"), self.codex_rollout()]
        cold = self.cached_families(self.since)
        for path in paths:
            path.unlink()
        with (patch.object(reader, "_read_transcript", side_effect=AssertionError("deleted Claude parse")),
              patch.object(reader, "_read_codex_file", side_effect=AssertionError("deleted Codex parse"))):
            self.assertEqual(self.cached_families(self.since), cold)
            self.assertEqual(self.cached_families(self.until + timedelta(seconds=2)), [])

    def test_compressed_transcripts_share_the_logical_entry_without_parsing(self) -> None:
        paths = [self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl"), self.codex_rollout()]
        cold = self.cached_families(self.since)
        with closing(cache_module.TranscriptCache()) as cache:
            for path in paths:
                cache.mark_final(path)
                _ = path.rename(path.with_suffix(".jsonl.zst"))
        with (patch.object(reader, "_read_transcript", side_effect=AssertionError("compressed Claude parse")),
              patch.object(reader, "_read_codex_file", side_effect=AssertionError("compressed Codex parse"))):
            self.assertEqual(self.cached_families(self.since), cold)

    def test_uncached_compressed_files_count_as_skipped(self) -> None:
        path = self.projects / "project/unread.jsonl.zst"
        path.parent.mkdir()
        _ = path.write_bytes(b"not parsed in this phase")
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.cached_families(self.since), [])
        self.assertIn("files 1 of 1; skipped 1", errors.getvalue())

    def test_cache_bound_evicts_the_least_recently_used_reproducible_summary(self) -> None:
        self.assert_cache_eviction(deleted=False)

    def test_cache_bound_keeps_a_summary_whose_source_is_gone(self) -> None:
        self.assert_cache_eviction(deleted=True)

    def assert_cache_eviction(self, *, deleted: bool) -> None:
        configuration = self.root / "configuration/context_economy.conf"
        _ = configuration.write_text(configuration.read_text().replace(
            "cache_max_megabytes = 512", "cache_max_megabytes = 1"))
        paths = [self.projects / f"summary-{index}.jsonl" for index in range(2)]
        for path in paths:
            _ = path.write_text("source\n")
        summary = cache_module.ParseCheckpoint(7, "x" * 700_000)
        with closing(cache_module.TranscriptCache()) as cache:
            with patch.object(time, "time_ns", side_effect=[1, 2, 3]):
                for path in paths:
                    self.assertEqual(cache.read(path, "claude", lambda path, held: summary), summary)
                if deleted:
                    paths[0].unlink()
                else:
                    self.assertEqual(cache.read(paths[0], "claude", lambda path, held: summary), summary)
        with closing(cache_module.TranscriptCache()) as cache:
            parse = Mock(return_value=summary)
            parse_file = cast(Callable[[Path, cache_module.ParseCheckpoint], cache_module.ParseCheckpoint], parse)
            self.assertEqual(cache.read(paths[0], "claude", parse_file), summary)
            parse.assert_not_called()
            self.assertEqual(cache.read(paths[1], "claude", parse_file), summary)
            parse.assert_called_once_with(paths[1].resolve(), cache_module.ParseCheckpoint())

    def test_progress_rewrites_terminal_lines_and_keeps_pipe_lines_separate(self) -> None:
        for terminal, separator in ((True, "\r"), (False, "\n")):
            with (self.subTest(terminal=terminal), redirect_stderr(io.StringIO()) as errors,
                  patch.object(errors, "isatty", return_value=terminal),
                  patch.object(time, "monotonic", side_effect=[0, 2, 4])):
                progress = cache_module.FileProgress("fixture-machine", "claude", 2)
                progress.advance()
                progress.advance()
            self.assertEqual(errors.getvalue(),
                "fixture-machine claude: files 1 of 2; skipped 0" + separator
                + "fixture-machine claude: files 2 of 2; skipped 0\n")

    def test_mac_progress_reaches_stderr_without_changing_the_result_document(self) -> None:
        program = ("import sys; print('mac claude: files 1 of 1; skipped 0', file=sys.stderr); "
                   + "print('{\"type\":\"timeline\",\"entries\":[]}')")
        with (patch.dict(os.environ, {"CONTEXT_ECONOMY_MAC_COMMAND": json.dumps([sys.executable, "-I", "-c", program])}),
              redirect_stderr(io.StringIO()) as errors):
            result = reader.read_mac(self.since, self.until)
        self.assertEqual(result, reader.MacSessions([], []))
        self.assertEqual(errors.getvalue(), "mac claude: files 1 of 1; skipped 0\n")

    def test_mac_payload_carries_settings_and_emit_ignores_missing_remote_keys(self) -> None:
        _ = self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl")
        rollout = self.codex_rollout()
        _ = self.settings_history()
        configuration = self.root / "configuration/context_economy.conf"
        _ = configuration.write_text(configuration.read_text(encoding="utf-8").replace(
            "cache_max_megabytes = 512", "cache_max_megabytes = 3").replace(
            "progress_seconds = 1", "progress_seconds = 7"), encoding="utf-8")
        with patch.dict(os.environ, {
            "CONTEXT_ECONOMY_MAC_PROJECTS_ROOT": str(self.projects),
            "CONTEXT_ECONOMY_MAC_CODEX_ROOTS": json.dumps([str(rollout.parents[3])]),
            "CONTEXT_ECONOMY_MAC_CHECKOUT": str(self.checkout),
        }):
            payload = cast(Callable[[datetime, datetime], str], getattr(reader, "_mac_payload"))
            request = cast(dict[str, object], json.loads(payload(self.since, self.until)))
        self.assertEqual(request["cache_max_megabytes"], 3)
        self.assertEqual(request["progress_seconds"], 7)
        _ = configuration.write_text("\n".join(line for line in configuration.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("cache_max_megabytes =", "progress_seconds ="))) + "\n", encoding="utf-8")
        with (redirect_stdout(io.StringIO()) as output,
              patch.object(reader, "TranscriptCache", wraps=cache_module.TranscriptCache) as cache,
              patch.object(reader, "FileProgress", wraps=cache_module.FileProgress) as progress):
            reader.emit_mac(request)
        settings = cache_module.TranscriptReadSettings(3, 7)
        self.assertEqual(cache.call_args_list, [call(settings), call(settings)])
        self.assertEqual(progress.call_args_list, [call("mac", "claude", 1, settings), call("mac", "codex", 1, settings)])
        rows = [cast(dict[str, object], json.loads(line)) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0]["type"], "timeline")
        self.assertEqual([(row["machine"], row["family"], row["agent_id"]) for row in rows[1:]],
                         [("mac", "claude", "parent-fixture"), ("mac", "codex", "codex-fixture")])

    def test_mac_payload_reads_both_families_and_the_remote_timeline_without_writes(self) -> None:
        parent = self.transcript("parent-fixture.jsonl", "project/parent-fixture.jsonl")
        child = self.transcript("agent-child-fixture.jsonl", "project/parent-fixture/subagents/agent-child-fixture.jsonl")
        rollout = self.codex_rollout()
        commits, _, _ = self.settings_history()
        before = {path: path.read_bytes() for path in (parent, child, rollout, self.checkout / "settings.json")}
        with patch.dict(os.environ, {
            "CONTEXT_ECONOMY_MAC_COMMAND": json.dumps([sys.executable, "-I", "-c"]),
            "CONTEXT_ECONOMY_MAC_PROJECTS_ROOT": str(self.projects),
            "CONTEXT_ECONOMY_MAC_CODEX_ROOTS": json.dumps([str(rollout.parents[3])]),
            "CONTEXT_ECONOMY_MAC_CHECKOUT": str(self.checkout),
        }):
            result = reader.read_mac(self.since, self.until)
        self.assertIsInstance(result, reader.MacSessions, result)
        assert isinstance(result, reader.MacSessions)
        self.assertCountEqual([(session.machine, session.family, session.agent_id) for session in result.sessions], [
            ("mac", "claude", "parent-fixture"), ("mac", "claude", "child-fixture"), ("mac", "codex", "codex-fixture"),
        ])
        self.assertEqual([entry.commit for entry in result.timeline], commits)
        codex = next(session for session in result.sessions if session.family == "codex")
        self.assertEqual((codex.requests[0].model, codex.requests[0].cli_version, codex.requests[0].context),
                         ("gpt-6.1-sol", "0.101.0", 1000))
        self.assertIsInstance(codex.compactions[0].pre_tokens, reader.SizeNotReported)
        claude = next(session for session in result.sessions if session.agent_id == "parent-fixture")
        self.assertEqual((claude.compactions[0].pre_tokens, claude.compactions[0].post_tokens), (9000, 400))
        self.assertIsInstance(claude.compactions[2].post_tokens, reader.SizeNotReported)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_mac_with_no_sessions_returns_its_timeline(self) -> None:
        commits, _, _ = self.settings_history()
        with patch.dict(os.environ, {
            "CONTEXT_ECONOMY_MAC_COMMAND": json.dumps([sys.executable, "-I", "-c"]),
            "CONTEXT_ECONOMY_MAC_PROJECTS_ROOT": str(self.projects),
            "CONTEXT_ECONOMY_MAC_CODEX_ROOTS": json.dumps([str(self.root / "empty-codex")]),
            "CONTEXT_ECONOMY_MAC_CHECKOUT": str(self.checkout),
        }):
            result = reader.read_mac(self.since, self.until)
        self.assertIsInstance(result, reader.MacSessions, result)
        assert isinstance(result, reader.MacSessions)
        self.assertEqual(result.sessions, [])
        self.assertEqual([entry.commit for entry in result.timeline], commits)

    def test_mac_reports_command_failures_and_rejects_malformed_records(self) -> None:
        configuration = self.root / "configuration/context_economy.conf"
        _ = configuration.write_text(configuration.read_text(encoding="utf-8").replace(
            "mac_timeout_seconds = 300", "mac_timeout_seconds = 1"), encoding="utf-8")
        valid: dict[str, object] = {
            "type": "session", "machine": "mac", "family": "codex", "session_id": "fixture", "agent_id": "fixture",
            "first_record_at": None, "requests": [], "starts": [], "compactions": [],
        }
        header = json.dumps({"type": "timeline", "entries": []}) + "\n"
        commands = [
            ("import sys; print('fixture unreachable', file=sys.stderr); sys.exit(7)", "fixture unreachable"),
            ("import signal; signal.pause()", "timed out after 1 seconds"),
        ]
        for output in ("{broken", "", header + json.dumps({**valid, "family": "other"}),
                       header + json.dumps({**valid, "requests": [{"at": "invalid"}]}),
                       header + json.dumps({**valid, "compactions": [{"at": self.since.isoformat(), "trigger": None,
                                                  "kind": "unknown", "pre_tokens": False, "post_tokens": None}]}),
                       header + json.dumps({**valid, "compactions": [{"at": self.since.isoformat(), "trigger": None,
                                                  "kind": "unknown", "pre_tokens": -1, "post_tokens": None}]}),
                       header + json.dumps({**valid, "requests": [{"at": self.since.isoformat(), "model": "gpt-6.1-sol",
                                                  "cli_version": "0.101.0", "input": -1, "cache_read": 0,
                                                  "cache_write_5m": 0, "cache_write_1h": 0, "output": 0}]}),
                       header + json.dumps({**valid, "starts": [{"at": self.since.isoformat(), "source": "other"}]})):
            commands.append((f"import sys; sys.stdout.write({output!r})",
                             "Mac data" if output != "{broken" else "property name"))
        for program, reason in commands:
            with self.subTest(program=program), patch.dict(os.environ, {
                "CONTEXT_ECONOMY_MAC_COMMAND": json.dumps([sys.executable, "-I", "-c", program]),
            }):
                result = reader.read_mac(self.since, self.until)
                self.assertIsInstance(result, reader.MacReadFailed)
                assert isinstance(result, reader.MacReadFailed)
                self.assertIn(reason, result.reason)


if __name__ == "__main__":
    _ = unittest.main()
