"""Read context events and settings from isolated transcript, Git, and ledger fixtures."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
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
            + "active_gap_seconds = 600\n"
            + "# whole: 1..\nmac_timeout_seconds = 300\n", encoding="utf-8")
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
        with patch.object(Path, "open", autospec=True, side_effect=Path.open) as opened:
            sessions = reader.read_codex([shared, numbered, shared / "2026"], self.since, self.until, "fixture-machine")
            opened.assert_called_once()
        session, = sessions
        self.assertEqual([item.context for item in session.requests], [1000, 300, 350])

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
