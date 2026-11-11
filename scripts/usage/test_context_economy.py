"""Test context quantities and saved comparisons with isolated session fixtures."""
from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from functools import cached_property
import io
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import tempfile
from typing import Literal, TextIO, cast, final, override
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.usage import collect, context_economy as economy
from scripts.usage.context_economy_read import (
    CompactionEvent, MacReadFailed, MacSessions, Request, Session, SettingsSet, SizeNotReported, Start,
)


BEGIN = datetime(2026, 11, 2, tzinfo=timezone.utc)
MODEL = "claude-sonnet-4-5"


def at(seconds: int) -> datetime:
    return BEGIN + timedelta(seconds=seconds)


def request(seconds: int, scale: int, *, version: str = "2.1.0", model: str = MODEL) -> Request:
    """Each scale adds 100 context tokens and 97.5 weighted tokens."""
    return Request(at(seconds), model, version, 20 * scale, 50 * scale, 10 * scale, 20 * scale, 4 * scale)


def settings(seconds: int = -60, *, enabled: bool = True, window: int = 100000) -> SettingsSet:
    return SettingsSet(at(seconds), f"settings-{seconds}-{enabled}-{window}", {
        "autoCompactWindow": window,
        "precomputeCompactionEnabled": enabled,
        "model": MODEL,
        "commands/unit/direct.md": {"bytes": 100, "sha": "111111111111"},
    })


def quantity_session() -> Session:
    """Three compactions span six active minutes and one inactive gap."""
    return Session("fixture", "claude", "quantities", "quantities", [
        request(60, 1), request(120, 2), request(180, 3), request(240, 4),
        request(300, 5), request(360, 6), request(1560, 7), request(1620, 8),
    ], [
        CompactionEvent(at(150), "auto", 1000, 100, "normal"),
        CompactionEvent(at(330), "manual", 2000, 200, "precomputed"),
        CompactionEvent(at(1590), None, SizeNotReported(), SizeNotReported(), "unknown"),
    ], [Start(at(0), "startup", "2.1.0")], at(0))


def resumed_session() -> Session:
    """The first two requests keep startup settings until the resume."""
    return Session("fixture", "claude", "resumed", "resumed", [
        request(60, 1), request(660, 2), request(1260, 3), request(1320, 4),
    ], [
        CompactionEvent(at(690), "auto", 1000, 100, "precomputed"),
        CompactionEvent(at(1290), "auto", 2000, 200, "normal"),
    ], [Start(at(0), "startup", "2.1.0"), Start(at(1200), "resume", "2.1.1")], at(0))


def sample_session(name: str, count: int, scale: int = 1, *, version: str = "2.1.0", model: str = MODEL,
                   offset: int = 0) -> Session:
    return Session("fixture", "claude", name, name, [
        request(offset + 60 * number, scale, version=version, model=model) for number in range(1, count + 1)
    ], [], [Start(at(offset), "startup", version)], at(offset))


def quantity_rows(output: str, quantity: str) -> list[list[str]]:
    """Read the data rows in one quantity's Markdown table."""
    rows: list[list[str]] = []
    active = False
    for line in output.splitlines():
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells[0] == quantity:
                rows.append(cells)
        if line.startswith("#"):
            if active:
                break
            active = quantity in line
        elif active and line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[2:] if active else rows


@final
class ContextEconomyTests(unittest.TestCase):
    @cached_property
    def root(self) -> Path:
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    @property
    def store(self) -> Path:
        return self.root / "state/usage.db"

    @property
    def projects(self) -> Path:
        return self.root / "projects"

    @property
    def checkout(self) -> Path:
        return self.root / "checkout"

    @property
    def saved(self) -> Path:
        return self.root / "state/context_economy"

    @override
    def setUp(self) -> None:
        for folder in (self.projects, self.checkout, self.root / "config", self.root / "home", self.store.parent,
                       self.root / "codex-sessions"):
            folder.mkdir(parents=True, exist_ok=True)
        self.configure()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "CONF_DIR": str(self.root / "config"),
            "USAGE_STATE_DIR": str(self.root / "state"), "CONTEXT_ECONOMY_CHECKOUT": str(self.checkout),
            "USER_ZONE": "UTC", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        })))
        _ = cast(object, self.enterContext(patch.object(economy, "read_mac", return_value=MacSessions([], []))))
        _ = cast(object, self.enterContext(patch.object(socket, "gethostname", return_value="fixture")))

    def configure(self, *, min_requests: int = 1, active_gap_seconds: int = 600) -> None:
        _ = (self.root / "config/context_economy.conf").write_text(
            "settings_keys = autoCompactWindow precomputeCompactionEnabled model\n"
            + "files = commands/unit/direct.md\n"
            + f"# whole: 1..\nmin_requests = {min_requests}\n"
            + f"# whole: 1..\nactive_gap_seconds = {active_gap_seconds}\n"
            + "# whole: 1..\nhourly_recent_hours = 1\n# whole: 1..\nhourly_baseline_days = 1\n"
            + "# whole: 1..\ncache_max_megabytes = 512\n# whole: 1..\nprogress_seconds = 1\n", encoding="utf-8")

    def report(self, sessions: list[Session], timeline: list[SettingsSet], *, by: Literal["settings", "week"] = "settings",
               role: str = "") -> economy.Report:
        return economy.build_report(sessions, {"fixture": timeline}, self.store, at(-60), at(14 * 86400), by=by, role=role)

    def test_hourly_rates_compare_complete_hours_with_the_baseline(self) -> None:
        requests = [("a", "natedev", "director", -7200, 1_000_000), ("a", "natedev", "director", -3600, 3_000_000),
                    ("b", "natedev", "director", -3599, 3_000_000), ("b", "mac", "director", -3598, 3_000_000),
                    ("c", "natedev", "showrunner", -3597, 9_000_000), ("a", "natedev", "director", 60, 9_000_000)]
        hour = at(0).replace(minute=0, second=0, microsecond=0)
        with closing(sqlite3.connect(self.store)) as database:
            _ = database.executescript(collect.SCHEMA)
            for index, (agent, machine, role, offset, tokens) in enumerate(requests):
                _ = database.execute(
                    "INSERT INTO turns (machine, family, kind, parent_id, agent_id, turn_key, at, model, input, cache_write_5m, "
                    + "cache_write_1h, cache_read, output, before_first_edit, role, attribution, lens_state) "
                    + "VALUES (?, 'claude', 'session', '', ?, ?, ?, 'claude-opus-5-5', ?, 0, 0, 0, 0, 0, ?, 'recorded', 'not_applicable')",
                    (machine, agent, str(index), (hour + timedelta(seconds=offset)).isoformat(), tokens, role))
            database.commit()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(economy.main(["hourly", "--store", str(self.store)]), 0)
        rows = output.getvalue().splitlines()
        self.assertIn(f"Ledger as of {hour + timedelta(seconds=60):%a %H:%M} UTC", rows[0])
        self.assertEqual(rows[4:], ["| normal (median) | 1 | 1.0M | 1.00M |", "| busy (p75) | 1 | 1.0M | 1.00M |",
                                    f"| {hour - timedelta(hours=1):%a %H:00} | 3 | 9.0M | 3.00M |"])

    def record_role(self, session: Session, role: str) -> None:
        with closing(sqlite3.connect(self.store)) as database:
            _ = database.execute("CREATE TABLE IF NOT EXISTS turns (machine TEXT, family TEXT, parent_id TEXT, "
                                 + "agent_id TEXT, at TEXT, role TEXT)")
            parent = session.session_id if session.session_id != session.agent_id else ""
            _ = database.execute("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?)",
                                 (session.machine, session.family, parent, session.agent_id, at(0).isoformat(), role))
            database.commit()

    def cli(self, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        paths = ["--projects-root", str(self.projects), "--store", str(self.store),
                 "--checkout", str(self.checkout), "--saved-results", str(self.saved)]
        if arguments[0] != "trend":
            paths.extend(("--codex-root", str(self.root / "codex-sessions")))
        with redirect_stdout(output), redirect_stderr(errors):
            status = economy.main([*arguments, *paths])
        return status, output.getvalue(), errors.getvalue()

    def git(self, *arguments: str, seconds: int = -120) -> str:
        result = subprocess.run([
            "git", "-C", str(self.checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *arguments,
        ], env=dict(os.environ, GIT_AUTHOR_DATE=at(seconds).isoformat(), GIT_COMMITTER_DATE=at(seconds).isoformat()),
            check=True, capture_output=True, text=True, timeout=15)
        return result.stdout.strip()

    def command_inputs(self, session: Session) -> None:
        """Write transcripts and Git inputs only in the temporary fixture folders."""
        _ = self.git("init", "--initial-branch=main")
        _ = (self.checkout / "settings.json").write_text(json.dumps({
            "autoCompactWindow": 100000, "precomputeCompactionEnabled": False, "model": MODEL,
        }), encoding="utf-8")
        tracked = self.checkout / "commands/unit/direct.md"
        tracked.parent.mkdir(parents=True)
        _ = tracked.write_text("Fixture instructions.\n", encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Record fixture settings")
        folder = self.projects / "fixture-project"
        folder.mkdir()
        records: list[dict[str, object]] = []
        for number, start in enumerate(session.starts):
            records.append({"type": "attachment", "timestamp": start.at.isoformat(), "version": start.cli_version,
                            "attachment": {"type": "hook_success", "hookEvent": "SessionStart",
                                           "hookName": f"SessionStart:{start.source}", "toolUseID": f"start-{number}"}})
        for number, item in enumerate(session.requests):
            records.append({"type": "assistant", "timestamp": item.at.isoformat(), "requestId": f"request-{number}",
                            "version": item.cli_version, "message": {"id": f"message-{number}", "model": item.model,
                            "usage": {"input_tokens": item.input, "cache_read_input_tokens": item.cache_read,
                                      "cache_creation": {"ephemeral_5m_input_tokens": item.cache_write_5m,
                                                         "ephemeral_1h_input_tokens": item.cache_write_1h},
                                      "output_tokens": item.output}}})
        transcript = folder / f"{session.session_id}.jsonl"
        _ = transcript.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    def test_each_quantity_uses_the_session_events_and_all_token_fields(self) -> None:
        expected = {
            "fresh_start": [100.0],
            "after_compaction": [300.0, 600.0, 800.0],
            "at_compaction": [1000.0, 2000.0],
            "context_per_request": [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0],
            "cost_per_request": [97.5, 195.0, 292.5, 390.0, 487.5, 585.0, 682.5, 780.0],
            "requests_per_cycle": [3.0, 2.0],
            "compactions_per_active_hour": [30.0],
            "precomputed_share": [0.5],
        }
        actual = economy.quantity_samples(quantity_session(), active_gap_seconds=600)
        self.assertEqual(actual, expected)

    def test_the_active_gap_threshold_excludes_equal_and_longer_gaps(self) -> None:
        for threshold, expected in ((60, []), (600, [30.0]), (1200, [30.0]), (1201, [90 / 13])):
            with self.subTest(threshold=threshold):
                actual = economy.quantity_samples(quantity_session(), active_gap_seconds=threshold)
                self.assertEqual(len(actual["compactions_per_active_hour"]), len(expected))
                for value, wanted in zip(actual["compactions_per_active_hour"], expected):
                    self.assertAlmostEqual(value, wanted)

    def test_each_quantity_reports_its_sample_size_and_distribution(self) -> None:
        expected = {
            "fresh_start": (1, 100.0, 100.0, 100.0, 100.0),
            "after_compaction": (3, 600.0, 1700 / 3, 450.0, 700.0),
            "at_compaction": (2, 1500.0, 1500.0, 1250.0, 1750.0),
            "context_per_request": (8, 450.0, 450.0, 275.0, 625.0),
            "cost_per_request": (8, 438.75, 438.75, 268.125, 609.375),
            "requests_per_cycle": (2, 2.5, 2.5, 2.25, 2.75),
            "compactions_per_active_hour": (1, 30.0, 30.0, 30.0, 30.0),
            "precomputed_share": (1, 0.5, 0.5, 0.5, 0.5),
        }
        original = quantity_session()
        resumed = replace(original, starts=[*original.starts, Start(at(240), "resume", "2.1.0")])
        for session in (original, resumed):
            with self.subTest(starts=len(session.starts)):
                report = self.report([session], [settings()])
                group, = report.groups
                for quantity, wanted in expected.items():
                    with self.subTest(quantity=quantity):
                        summary = group.quantities[quantity]
                        self.assertEqual(summary.n, wanted[0])
                        for value, target in zip((summary.median, summary.mean, summary.p25, summary.p75), wanted[1:]):
                            self.assertAlmostEqual(value, target)
                self.assertEqual(group.requests, 8)
                self.assertEqual(group.cli_versions, ("2.1.0",))
                output = economy.render_report(report)
                for quantity in expected:
                    self.assertEqual(len(quantity_rows(output, quantity)), 1, output)

    def test_requests_keep_the_latest_start_settings_until_the_next_start(self) -> None:
        old, new = settings(), settings(600, enabled=False)
        report = self.report([resumed_session()], [old, new])
        groups = {group.settings_commit: group for group in report.groups}
        self.assertEqual(set(groups), {old.commit, new.commit})
        for commit, requests, median, fresh_n in ((old.commit, 2, 150.0, 1), (new.commit, 2, 350.0, 0)):
            with self.subTest(commit=commit):
                group = groups[commit]
                self.assertEqual(group.requests, requests)
                self.assertEqual(group.quantities["context_per_request"].median, median)
                self.assertEqual(group.quantities["fresh_start"].n, fresh_n)
        self.assertEqual(groups[old.commit].quantities["precomputed_share"].median, 1.0)
        self.assertEqual(groups[new.commit].quantities["precomputed_share"].median, 0.0)
        after = groups[new.commit].quantities["after_compaction"]
        self.assertEqual((after.n, after.mean), (2, 350.0))
        cycle = groups[new.commit].quantities["requests_per_cycle"]
        self.assertEqual((cycle.n, cycle.mean), (1, 1.0))

    def test_a_request_without_a_window_start_uses_the_first_record_settings(self) -> None:
        before, after = settings(), settings(600, enabled=False)
        session = replace(resumed_session(), starts=[], first_record_at=at(0))
        group, = self.report([session], [before, after]).groups
        self.assertEqual(group.settings_commit, before.commit)
        self.assertEqual(group.requests, 4)
        self.assertEqual(group.quantities["context_per_request"].median, 250.0)

        session = replace(sample_session("command", 12, offset=900),
                          starts=[Start(at(-60), "startup", "2.1.0")], first_record_at=at(-60))
        self.command_inputs(session)
        first_commit = self.git("rev-parse", "HEAD")
        current = self.checkout / "settings.json"
        _ = current.write_text(json.dumps({"autoCompactWindow": 200000,
                               "precomputeCompactionEnabled": True, "model": MODEL}), encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Change the fixture settings", seconds=600)
        for anchor in ("earlier-start", "first-record"):
            with self.subTest(anchor=anchor):
                if anchor == "first-record":
                    transcript = self.projects / "fixture-project/command.jsonl"
                    records = transcript.read_text(encoding="utf-8").splitlines()
                    records[0] = json.dumps({"type": "system", "timestamp": at(-60).isoformat(), "version": "2.1.0"})
                    _ = transcript.write_text("\n".join(records) + "\n", encoding="utf-8")
                status, output, errors = self.cli("save", anchor, "--since", at(900).isoformat(),
                                                  "--until", at(1800).isoformat())
                self.assertEqual(status, 0, output + errors)
                restored = economy.read_result(self.saved / (anchor + ".json"))
                group, = restored.groups
                self.assertEqual(group.settings_commit, first_commit)
                self.assertEqual((restored.since, restored.until), (at(900), at(1800)))
                self.assertEqual(group.requests, 12)
                self.assertEqual(group.quantities["fresh_start"].n, 0)

    def test_disabled_precompute_with_only_normal_compactions_reports_zero_percent(self) -> None:
        session = replace(quantity_session(), compactions=[
            CompactionEvent(at(150), "auto", 1000, 100, "normal"),
            CompactionEvent(at(330), "auto", 2000, 200, "normal"),
        ])
        report = self.report([session], [settings(enabled=False)])
        group, = report.groups
        self.assertEqual(group.quantities["precomputed_share"].median, 0.0)
        rows = quantity_rows(economy.render_report(report), "precomputed_share")
        self.assertEqual(len(rows), 1)
        self.assertTrue(any(cell.endswith("%") and float(cell[:-1]) == 0 for cell in rows[0]), rows)

    def test_small_sessions_are_left_out_and_the_threshold_session_is_kept(self) -> None:
        sessions = [sample_session("below", 2, 9), sample_session("boundary", 3), sample_session("larger", 4, 2)]
        resumed = [replace(sample_session(name, count),
                           starts=[Start(at(0), "startup", "2.1.0"), Start(at(900), "resume", "2.1.0")])
                   for name, count in (("thirty", 30), ("nineteen", 19))]
        for minimum, inputs, requests, mean in ((3, sessions, 7, 1100 / 7), (20, resumed, 30, 100.0)):
            with self.subTest(minimum=minimum):
                self.configure(min_requests=minimum)
                report = self.report(inputs, [settings()])
                group, = report.groups
                self.assertEqual(report.left_out, 1)
                self.assertEqual(group.requests, requests)
                self.assertEqual(group.quantities["context_per_request"].mean, mean)
                self.assertRegex(economy.render_report(report), r"left out:\s*1\b")

    def test_settings_roles_and_models_form_separate_groups_and_role_filter(self) -> None:
        director = sample_session("director", 3)
        worker = sample_session("worker", 4, 2)
        other_model = sample_session("other-model", 5, 3, model="claude-opus-4-5")
        self.record_role(director, "unit director")
        self.record_role(worker, "worker")
        self.record_role(other_model, "worker")
        report = self.report([director, worker, other_model], [settings()])
        self.assertCountEqual([(group.role, group.model, group.requests) for group in report.groups], [
            ("unit director", MODEL, 3), ("worker", MODEL, 4), ("worker", "claude-opus-4-5", 5),
        ])
        filtered = self.report([director, worker, other_model], [settings()], role="unit director")
        group, = filtered.groups
        self.assertEqual((group.role, group.requests), ("unit director", 3))

    def test_compare_names_every_confounded_setting_version_and_model(self) -> None:
        baseline = self.report([sample_session("before", 12)], [settings()])
        changed = settings(enabled=False, window=200000)
        changed.values["commands/unit/direct.md"] = {"bytes": 200, "sha": "222222222222"}
        changed.values["model"] = "claude-opus-4-5"
        current = self.report([sample_session("after", 15, 2, version="2.2.0", model="claude-opus-4-5")], [changed])
        saved = economy.save_result("baseline", baseline, self.saved)
        output = economy.compare_reports(economy.read_result(saved), current)
        rows = quantity_rows(output, "context_per_request")
        self.assertEqual(len(rows), 1, output)
        cells = rows[0]
        self.assertIn("12", cells)
        self.assertIn("15", cells)
        self.assertTrue(any(cell.endswith("%") and float(cell.rstrip("%")) == 100 for cell in cells), rows)
        confounds = "\n".join(line for line in output.splitlines() if "confounds:" in line)
        for name in ("autoCompactWindow", "precomputeCompactionEnabled", "commands/unit/direct.md", "model",
                     "Claude Code version"):
            self.assertIn(name, confounds)

        explicit_null = replace(settings(), commit="explicit-null", values={
            **settings().values, "precomputeCompactionEnabled": None,
        })
        absent = replace(explicit_null, commit="absent", values={
            key: value for key, value in explicit_null.values.items() if key != "precomputeCompactionEnabled"
        })
        for previous, following, changed in ((explicit_null, absent, True), (absent, explicit_null, True),
                                             (explicit_null, explicit_null, False)):
            with self.subTest(previous=previous.commit, following=following.commit):
                before = self.report([sample_session("before", 12)], [previous])
                after = self.report([sample_session("after", 12, 2)], [following])
                output = economy.compare_reports(before, after)
                line = next(line for line in output.splitlines() if line.startswith("confounds:"))
                self.assertEqual("precomputeCompactionEnabled" in line, changed)

    def test_compare_requires_ten_samples_on_both_sides_for_a_percent_change(self) -> None:
        for before_n, after_n, too_few in ((9, 10, True), (10, 9, True), (10, 10, False)):
            with self.subTest(before_n=before_n, after_n=after_n):
                before = self.report([sample_session("before", before_n)], [settings()])
                after = self.report([sample_session("after", after_n, 2)], [settings()])
                output = economy.compare_reports(before, after)
                rows = quantity_rows(output, "context_per_request")
                self.assertEqual(len(rows), 1, output)
                self.assertIn(str(before_n), rows[0])
                self.assertIn(str(after_n), rows[0])
                self.assertEqual("too few" in " ".join(rows[0]), too_few)
                if not too_few:
                    self.assertTrue(any(cell.endswith("%") and float(cell[:-1]) == 100 for cell in rows[0]), rows)

    def test_save_round_trip_keeps_the_window_settings_versions_and_quantities(self) -> None:
        session = quantity_session()
        self.record_role(session, "unit director")
        report = self.report([session], [settings()])
        path = economy.save_result("before", report, self.saved)
        self.assertEqual(path, self.saved / "before.json")
        restored = economy.read_result(path)
        self.assertEqual((restored.since, restored.until, restored.left_out),
                         (report.since, report.until, report.left_out))
        before, = report.groups
        after, = restored.groups
        self.assertEqual((after.settings_values, after.settings_commit, after.role, after.model,
                          after.requests, after.cli_versions, after.quantities),
                         (before.settings_values, before.settings_commit, "unit director", MODEL,
                          8, before.cli_versions, before.quantities))
        self.assertEqual({path.name for path in self.saved.iterdir()}, {"before.json"})

        codex = replace(session, machine="mac", family="codex", session_id="codex", agent_id="codex")
        both = economy.build_report([session, codex], {"fixture": [settings()], "mac": [settings(enabled=False)]},
                                    self.store, at(0), at(3600))
        path = economy.save_result("both-machines", both, self.saved)
        restored = economy.read_result(path)
        self.assertEqual([(group.machine, group.family, group.requests) for group in restored.groups],
                         [("fixture", "claude", 8), ("mac", "codex", 8)])
        self.assertEqual(restored, both)
        raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        rows = cast(list[dict[str, object]], raw["groups"])
        self.assertEqual([(row["machine"], row["family"]) for row in rows], [("fixture", "claude"), ("mac", "codex")])

    def test_week_rows_mark_new_settings_versions_and_models_in_their_first_week(self) -> None:
        first = sample_session("week-one", 3)
        second = sample_session("week-two", 4, 2, version="2.2.0", model="claude-opus-4-5", offset=7 * 86400)
        report = self.report([first, second], [settings(), settings(6 * 86400, enabled=False)], by="week")
        self.assertEqual(sorted((group.week, group.requests) for group in report.groups),
                         [("2026-W45", 3), ("2026-W46", 4)])
        output = economy.render_report(report)
        for text in ("2026-W45", "2026-W46", "precomputeCompactionEnabled", "2.2.0", "claude-opus-4-5"):
            self.assertIn(text, output)
        mark = next(line for line in output.splitlines() if line.startswith("2026-W46:"))
        for text in ("new settings", "new CLI: 2.2.0", "new model: claude-opus-4-5"):
            self.assertIn(text, mark)
        self.assertNotIn("2.1.0", mark)

        middle = sample_session("same-week-change", 3, offset=3 * 86400)
        original_settings = replace(second, starts=[Start(at(0), "startup", "2.1.0")], first_record_at=at(0))
        mixed = self.report([first, middle, original_settings], [settings(), settings(2 * 86400, enabled=False)], by="week")
        mixed_output = economy.render_report(mixed)
        later_mark = next(line for line in mixed_output.splitlines() if line.startswith("2026-W46:"))
        self.assertNotIn("new settings", later_mark)
        self.assertIn("new CLI: 2.2.0", later_mark)
        self.assertIn("new model: claude-opus-4-5", later_mark)

    def test_cli_save_refuses_an_existing_name_and_keeps_the_saved_bytes(self) -> None:
        self.command_inputs(sample_session("command", 12))
        window = ["--since", at(0).isoformat(), "--until", at(3600).isoformat()]
        status, output, errors = self.cli("save", "baseline", *window)
        self.assertEqual(status, 0, output + errors)
        path = self.saved / "baseline.json"
        original = path.read_bytes()
        status, output, errors = self.cli("save", "baseline", *window)
        self.assertNotEqual(status, 0, output + errors)
        self.assertIn("baseline", output + errors)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(economy.read_result(path).groups), 1)

        def interrupted_dump(data: object, stream: TextIO, *, ensure_ascii: bool, indent: int) -> None:
            content = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
            _ = stream.write(content[:20])
            raise OSError("The fixture interrupted the write.")

        for point, interruption in (
            ("during-write", patch.object(json, "dump", side_effect=interrupted_dump)),
            ("before-publish", patch.object(os, "link", side_effect=OSError("The fixture interrupted publication."))),
        ):
            with self.subTest(point=point):
                report = economy.read_result(path)
                with interruption, self.assertRaises(OSError):
                    _ = economy.save_result(point, report, self.saved)
                self.assertIn("baseline", economy.trend(self.saved))
                recovered = economy.save_result(point, report, self.saved)
                self.assertEqual(economy.read_result(recovered), report)
                self.assertIn(point, economy.trend(self.saved))
                self.assertEqual(path.read_bytes(), original)

    def test_cli_compare_reads_the_saved_result_and_prints_both_sample_sizes(self) -> None:
        session = replace(sample_session("command", 15, 2),
                          starts=[Start(at(0), "startup", "2.1.0"), Start(at(600), "resume", "2.1.0")])
        self.command_inputs(session)
        _ = (self.checkout / "settings.json").write_text(json.dumps({
            "autoCompactWindow": 100000, "precomputeCompactionEnabled": True, "model": MODEL,
        }), encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Enable the fixture setting", seconds=600)
        baseline = self.report([sample_session("before", 12)], [settings(enabled=False)])
        path = economy.save_result("baseline", baseline, self.saved)
        original = path.read_bytes()
        cases: tuple[tuple[list[str], int], ...] = (
            ([], 15), (["--where", "precomputeCompactionEnabled=false"], 9),
            (["--where", "precomputeCompactionEnabled=true", "--where", f'model="{MODEL}"'], 6),
        )
        for predicates, current_n in cases:
            with self.subTest(predicates=predicates):
                status, output, errors = self.cli("compare", "baseline", "--family", "claude", *predicates,
                                                  "--since", at(0).isoformat(), "--until", at(3600).isoformat())
                self.assertEqual(status, 0, output + errors)
                rows = quantity_rows(output, "context_per_request")
                self.assertEqual(len(rows), 1, output)
                self.assertEqual(rows[0][-2:], ["12", str(current_n)])
                self.assertEqual(path.read_bytes(), original)

    def test_cli_trend_uses_saved_time_order_after_the_source_files_are_gone(self) -> None:
        self.command_inputs(sample_session("command", 12))
        first = self.report([sample_session("first", 12)], [settings()])
        second = replace(self.report([sample_session("second", 12, 2)], [settings(enabled=False)]),
                         since=at(86400), until=at(2 * 86400))
        first_path = economy.save_result("z-earlier", first, self.saved)
        second_path = economy.save_result("a-later", second, self.saved)
        legacy = cast(dict[str, object], json.loads(first_path.read_text(encoding="utf-8")))
        del legacy["mac"]
        _ = first_path.write_text(json.dumps(legacy), encoding="utf-8")
        before = {path: path.read_bytes() for path in (first_path, second_path)}
        for transcript in self.projects.rglob("*.jsonl"):
            transcript.unlink()
        status, output, errors = self.cli("trend")
        self.assertEqual(status, 0, output + errors)
        self.assertLess(output.index("z-earlier"), output.index("a-later"), output)
        self.assertIn("100", output)
        self.assertIn("200", output)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        columns = next(line for line in output.splitlines() if line.startswith("| result |")).strip("|").split("|")
        mac_column = [column.strip() for column in columns].index("mac")
        for name in ("z-earlier", "a-later"):
            row = next(line for line in output.splitlines() if line.startswith(f"| {name} |"))
            self.assertEqual(row.strip("|").split("|")[mac_column].strip(), "")

    def test_cli_report_uses_the_user_zone_and_leaves_no_saved_result(self) -> None:
        self.command_inputs(sample_session("command", 12))
        with patch.dict(os.environ, {"USER_ZONE": "America/New_York"}):
            status, output, errors = self.cli("report", "--since", "2026-11-01T19:00:00",
                                             "--until", "2026-11-01T20:00:00")
        self.assertEqual(status, 0, output + errors)
        self.assertIn("2026-11-01", output)
        self.assertTrue("-05:00" in output or "EST" in output, output)
        self.assertEqual(len(quantity_rows(output, "context_per_request")), 1, output)

    def test_each_machine_uses_its_own_timeline_for_the_same_commits(self) -> None:
        on, off = settings(), settings(600, enabled=False)
        mac_on, mac_off = replace(on, at=at(-120)), replace(off, at=at(1800))
        local = replace(sample_session("same-id", 4, offset=900), machine="natedev")
        remote = replace(local, machine="mac")
        report = economy.build_report([local, remote], {"natedev": [on, off], "mac": [mac_on, mac_off]},
                                      self.store, at(0), at(3600))
        self.assertCountEqual([(group.machine, group.settings_commit, group.requests) for group in report.groups],
                              [("natedev", off.commit, 4), ("mac", on.commit, 4)])
        self.assertEqual(report.role_quantities["unknown"]["context_per_request"].n, 8)
        output = economy.render_report(report)
        rows = quantity_rows(output, "context_per_request")
        self.assertEqual({row[0] for row in rows}, {"mac", "natedev"})
        self.assertIn("| machine | family |", output)

    def test_unweighted_models_keep_context_samples_without_cost_samples(self) -> None:
        session = replace(sample_session("codex", 4), family="codex", requests=[
            Request(at(60), "gpt-6.1-sol", "0.9", 80, 20, 0, 0, 5),
            Request(at(120), "codex-model-unweighted", "0.9", 180, 20, 0, 0, 5),
            Request(at(180), "codex-model-unweighted", "0.9", 280, 20, 0, 0, 5),
            Request(at(240), "gpt-6.1-sol", "0.9", 380, 20, 0, 0, 5),
        ], compactions=[CompactionEvent(at(150), None, SizeNotReported(), SizeNotReported(), "unknown")])
        report = self.report([session], [settings()])
        groups = {group.model: group for group in report.groups}
        unknown = groups["codex-model-unweighted"]
        self.assertEqual(unknown.requests, 2)
        self.assertEqual(unknown.quantities["context_per_request"].n, 2)
        self.assertEqual(unknown.quantities["context_per_request"].mean, 250)
        self.assertEqual(unknown.quantities["cost_per_request"].n, 0)
        self.assertEqual(unknown.quantities["at_compaction"].n, 0)
        known = groups["gpt-6.1-sol"].quantities["cost_per_request"]
        self.assertEqual((known.n, known.mean), (2, 256))
        self.assertEqual(report.role_quantities["unknown"]["context_per_request"].n, 4)
        self.assertEqual(report.role_quantities["unknown"]["cost_per_request"].n, 2)
        output = economy.render_report(report)
        self.assertIn("cost not weighted: 2 requests (codex-model-unweighted)", output)
        path = economy.save_result("unweighted", report, self.saved)
        self.assertEqual(economy.read_result(path), report)

    def test_comparisons_name_one_side_machines_and_families_and_pool_samples(self) -> None:
        local = sample_session("local", 12)
        remote = replace(sample_session("remote", 15, 2), machine="mac", family="codex")
        before = self.report([local], [settings()])
        after = economy.build_report([local, remote], {"fixture": [settings()], "mac": [settings()]},
                                     self.store, at(0), at(3600))
        output = economy.compare_reports(before, after)
        confounds = next(line for line in output.splitlines() if line.startswith("confounds:"))
        self.assertIn("machine=mac (current only)", confounds)
        self.assertIn("family=codex (current only)", confounds)
        row, = quantity_rows(output, "context_per_request")
        self.assertIn("12", row)
        self.assertIn("27", row)
        reverse = economy.compare_reports(after, before)
        self.assertIn("machine=mac (saved only)", reverse)
        self.assertIn("family=codex (saved only)", reverse)

    def test_where_matches_off_sets_across_file_changes_and_explicit_null(self) -> None:
        on, off = settings(), settings(600, enabled=False)
        off = replace(off, values={**off.values, "fixtureValue": {"flags": [False, {"enabled": True}], "count": 1}})
        file_change = replace(off, at=at(1200), commit="file-change", values={
            **off.values, "commands/unit/direct.md": {"bytes": 200, "sha": "222222222222"},
        })
        missing = replace(file_change, at=at(1800), commit="missing-value", values={
            "precomputeCompactionEnabled": None, "autoCompactWindow": 100000,
        })
        absent = replace(missing, at=at(2400), commit="absent-key", values={"autoCompactWindow": 100000})
        sessions = [sample_session(str(offset), 3, offset=offset) for offset in (0, 600, 1200, 1800, 2400)]
        timelines = {"fixture": [on, off, file_change, missing, absent]}
        for predicates, expected in (
            ([economy.SettingsValueMatch("precomputeCompactionEnabled", False)], {off.commit, file_change.commit}),
            ([economy.SettingsValueMatch("precomputeCompactionEnabled", None)], {missing.commit}),
            ([economy.SettingsValueMatch("precomputeCompactionEnabled", 0)], set[str]()),
            ([economy.SettingsValueMatch("precomputeCompactionEnabled", 1)], set[str]()),
            ([economy.SettingsValueMatch("fixtureValue", {"flags": [False, {"enabled": True}], "count": 1.0})],
             {off.commit, file_change.commit}),
            ([economy.SettingsValueMatch("fixtureValue", {"flags": [0, {"enabled": True}], "count": 1})], set[str]()),
            ([economy.SettingsValueMatch("fixtureValue", {"flags": [False, {"enabled": 1}], "count": 1})], set[str]()),
            ([economy.SettingsValueMatch("precomputeCompactionEnabled", False),
              economy.SettingsValueMatch("commands/unit/direct.md", {"bytes": 200, "sha": "222222222222"})],
             {file_change.commit}),
        ):
            with self.subTest(expected=expected):
                report = economy.build_report(sessions, timelines, self.store, at(0), at(3600), where=predicates)
                self.assertEqual({group.settings_commit for group in report.groups}, expected)
                self.assertEqual(sum(group.requests for group in report.groups), 3 * len(expected))

    def test_cli_where_selects_settings_values_for_report_and_save(self) -> None:
        session = sample_session("command", 20)
        self.command_inputs(session)
        path = self.checkout / "settings.json"
        first = self.git("rev-parse", "HEAD")
        tracked = self.checkout / "commands/unit/direct.md"
        _ = tracked.write_text("The fixture changes the command.\n", encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Change the tracked command", seconds=300)
        second = self.git("rev-parse", "HEAD")
        _ = path.write_text(json.dumps({"precomputeCompactionEnabled": None, "model": MODEL}), encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Record an explicit null", seconds=600)
        third = self.git("rev-parse", "HEAD")
        _ = path.write_text(json.dumps({"model": MODEL}), encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Remove the fixture setting", seconds=960)
        session = replace(session, starts=[Start(at(0), "startup", "2.1.0"),
                                          Start(at(300), "resume", "2.1.0"),
                                          Start(at(600), "resume", "2.1.0"),
                                          Start(at(960), "resume", "2.1.0")])
        window = ["--since", at(0).isoformat(), "--until", at(1260).isoformat()]
        with patch.object(economy, "read_claude", return_value=[session]):
            for name, selected, expected, requests in (
                ("off", "false", {first, second}, 9), ("null", "null", {third}, 6), ("zero", "0", set[str](), 0),
            ):
                with self.subTest(name=name):
                    predicates = ["--where", f"precomputeCompactionEnabled={selected}", "--where", f'model="{MODEL}"']
                    status, output, errors = self.cli("report", *window, *predicates)
                    self.assertEqual(status, 0, output + errors)
                    self.assertEqual(len(quantity_rows(output, "context_per_request")), len(expected), output)
                    status, output, errors = self.cli("save", name, *window, *predicates)
                    self.assertEqual(status, 0, output + errors)
                    restored = economy.read_result(self.saved / f"{name}.json")
                    self.assertEqual({group.settings_commit for group in restored.groups}, expected)
                    self.assertEqual(sum(group.requests for group in restored.groups), requests)

    def test_cli_calls_each_reader_once_and_filters_families_after_collection(self) -> None:
        local = sample_session("local", 3)
        codex = replace(sample_session("codex", 4, 2), family="codex")
        remote = replace(sample_session("remote", 5, 3), machine="mac", family="codex")
        self.command_inputs(local)
        for family, expected in (("all", [("claude", 3), ("codex", 4), ("codex", 5)]),
                                 ("claude", [("claude", 3)]), ("codex", [("codex", 4), ("codex", 5)])):
            with self.subTest(family=family):
                with (patch.object(socket, "gethostname", return_value="fixture"),
                      patch.object(economy, "read_claude", return_value=[local]) as claude_read,
                      patch.object(economy, "read_codex", return_value=[codex]) as codex_read,
                      patch.object(economy, "read_mac", return_value=MacSessions([remote], [settings()])) as mac_read):
                    status, output, errors = self.cli("save", family, "--family", family,
                                                      "--since", at(0).isoformat(), "--until", at(3600).isoformat())
                self.assertEqual(status, 0, output + errors)
                restored = economy.read_result(self.saved / f"{family}.json")
                self.assertCountEqual([(group.family, group.requests) for group in restored.groups], expected)
                claude_read.assert_called_once()
                codex_read.assert_called_once_with([self.root / "codex-sessions"], at(0), at(3600), "fixture")
                mac_read.assert_called_once_with(at(0), at(3600))

    def test_a_failed_mac_read_keeps_local_numbers_and_an_empty_mac_is_successful(self) -> None:
        self.command_inputs(sample_session("command", 12))
        for name, mac, message, expected_mac in (
            ("mac-failed", MacReadFailed("fixture connection failed"), "mac: fixture connection failed",
             {"state": "failed", "reason": "fixture connection failed"}),
            ("mac-read", MacSessions([], []), "", {"state": "read"}),
        ):
            with self.subTest(mac=mac):
                with patch.object(economy, "read_mac", return_value=mac):
                    status, output, errors = self.cli("report", "--since", at(0).isoformat(),
                                                      "--until", at(3600).isoformat())
                self.assertEqual(status, 0, output + errors)
                self.assertEqual(len(quantity_rows(output, "context_per_request")), 1, output)
                if message:
                    self.assertIn(message, output)
                else:
                    self.assertNotIn("mac:", output)
                with patch.object(economy, "read_mac", return_value=mac):
                    status, output, errors = self.cli("save", name, "--since", at(0).isoformat(),
                                                      "--until", at(3600).isoformat())
                self.assertEqual(status, 0, output + errors)
                path = self.saved / f"{name}.json"
                saved = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
                self.assertEqual(saved["mac"], expected_mac)
                self.assertEqual(output.splitlines(), [str(path), message] if message else [str(path)])
                restored = economy.read_result(path)
                self.assertEqual(sum(group.requests for group in restored.groups), 12)
        self.configure(min_requests=20)
        for name, mac in (("empty-failed", MacReadFailed("fixture connection failed")),
                          ("empty-read", economy.MacReadSucceeded())):
            empty = replace(self.report([sample_session("below-threshold", 12)], [settings()]), mac=mac)
            _ = economy.save_result(name, empty, self.saved)
        status, output, errors = self.cli("trend")
        self.assertEqual(status, 0, output + errors)
        columns = next(line for line in output.splitlines() if line.startswith("| result |")).strip("|").split("|")
        mac_column = [column.strip() for column in columns].index("mac")
        for name, expected in (("mac-failed", "mac failed"), ("mac-read", ""),
                               ("empty-failed", "mac failed"), ("empty-read", "")):
            row = next(line for line in output.splitlines() if line.startswith(f"| {name} |"))
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            self.assertEqual(cells[mac_column], expected)
            if name.startswith("empty-"):
                names = [column.strip() for column in columns]
                self.assertEqual(cells[names.index("requests")], "0")
                self.assertEqual(cells[names.index("fresh_start"):], [""] * 8)
        status, output, errors = self.cli("trend", "--role", "unknown")
        self.assertEqual(status, 0, output + errors)
        self.assertNotIn("| empty-failed |", output)
        self.assertNotIn("| empty-read |", output)

    def test_last_week_saves_the_complete_user_week_once(self) -> None:
        self.command_inputs(sample_session("command", 12))
        for moment, zone, name, beginning, end in (
            ("2026-11-09T03:30:00+00:00", "America/New_York", "week-2026-W44",
             "2026-10-26T00:00:00-04:00", "2026-11-02T00:00:00-05:00"),
            ("2026-11-09T12:00:00+00:00", "UTC", "week-2026-W45",
             "2026-11-02T00:00:00+00:00", "2026-11-09T00:00:00+00:00"),
            ("2027-01-01T12:00:00+00:00", "UTC", "week-2026-W52",
             "2026-12-21T00:00:00+00:00", "2026-12-28T00:00:00+00:00"),
            ("2027-01-04T05:30:00+00:00", "America/New_York", "week-2026-W53",
             "2026-12-28T00:00:00-05:00", "2027-01-04T00:00:00-05:00"),
        ):
            with self.subTest(moment=moment, zone=zone):
                now = datetime.fromisoformat(moment).astimezone(ZoneInfo(zone))
                since, until = datetime.fromisoformat(beginning), datetime.fromisoformat(end)
                first_request = replace(request(0, 1), at=since + timedelta(days=2, hours=12))
                second_request = replace(request(0, 2), at=first_request.at + timedelta(minutes=1))
                session = replace(sample_session("week", 2), requests=[first_request, second_request],
                                  starts=[Start(since, "startup", "2.1.0")], first_record_at=since)
                with (patch.dict(os.environ, {"USER_ZONE": zone}),
                      patch.object(economy, "current_time", return_value=now),
                      patch.object(economy, "read_claude", return_value=[session]) as read):
                    status, output, errors = self.cli("save", "--last-week")
                self.assertEqual(status, 0, output + errors)
                path = self.saved / f"{name}.json"
                self.assertEqual(output.splitlines(), [str(path)])
                restored = economy.read_result(path)
                self.assertEqual((restored.since.isoformat(), restored.until.isoformat()), (beginning, end))
                self.assertEqual(sum(group.requests for group in restored.groups), 2)
                self.assertEqual(restored.role_quantities["unknown"]["context_per_request"].mean, 150)
                read.assert_called_once_with(self.projects, since, until, "fixture")
                original = path.read_bytes()
                with (patch.dict(os.environ, {"USER_ZONE": zone}),
                      patch.object(economy, "current_time", return_value=now),
                      patch.object(economy, "read_claude", side_effect=AssertionError("The saved week must need no input.")),
                      patch.object(economy, "read_codex", side_effect=AssertionError("The saved week must need no input.")),
                      patch.object(economy, "read_mac", side_effect=AssertionError("The saved week must need no input."))):
                    status, output, errors = self.cli("save", "--last-week")
                self.assertEqual(status, 0, output + errors)
                self.assertEqual(output.splitlines(), [str(path)])
                self.assertEqual(path.read_bytes(), original)

                legacy = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
                del legacy["mac"]
                _ = path.write_text(json.dumps(legacy), encoding="utf-8")
                original = path.read_bytes()
                with (patch.dict(os.environ, {"USER_ZONE": zone}),
                      patch.object(economy, "current_time", return_value=now),
                      patch.object(economy, "read_claude", side_effect=AssertionError("The old week must need no input.")),
                      patch.object(economy, "read_codex", side_effect=AssertionError("The old week must need no input.")),
                      patch.object(economy, "read_mac", side_effect=AssertionError("The old week must need no input."))):
                    status, output, errors = self.cli("save", "--last-week")
                self.assertEqual(status, 0, output + errors)
                self.assertEqual(output.splitlines(), [str(path)])
                self.assertEqual(path.read_bytes(), original)

                for outcome in (MacReadFailed("fixture Mac still asleep"), MacSessions([], [])):
                    with self.subTest(outcome=outcome):
                        failed = {**legacy, "mac": {"state": "failed", "reason": "fixture Mac asleep"}}
                        _ = path.write_text(json.dumps(failed), encoding="utf-8")
                        original = path.read_bytes()
                        changed = replace(session, requests=[replace(first_request, input=300, cache_read=0,
                            cache_write_5m=0, cache_write_1h=0), replace(second_request, input=400, cache_read=0,
                            cache_write_5m=0, cache_write_1h=0)])
                        with (patch.dict(os.environ, {"USER_ZONE": zone}),
                              patch.object(economy, "current_time", return_value=now),
                              patch.object(economy, "read_claude", return_value=[changed]) as reread,
                              patch.object(economy, "read_mac", return_value=outcome) as mac_read):
                            status, output, errors = self.cli("save", "--last-week")
                        self.assertEqual(status, 0, output + errors)
                        reread.assert_called_once_with(self.projects, since, until, "fixture")
                        mac_read.assert_called_once_with(since, until)
                        if isinstance(outcome, MacReadFailed):
                            self.assertEqual(path.read_bytes(), original)
                            self.assertEqual(output.splitlines(), [str(path), "mac: fixture Mac still asleep"])
                        else:
                            self.assertEqual(output.splitlines(), [str(path)])
                            refreshed = economy.read_result(path)
                            self.assertIsInstance(refreshed.mac, economy.MacReadSucceeded)
                            self.assertEqual(refreshed.role_quantities["unknown"]["context_per_request"].mean, 350)

        for arguments in (("extra-name",), ("--since", at(0).isoformat()), ("--until", at(3600).isoformat()),
                          ("--where", "precomputeCompactionEnabled=false"), ("--family", "claude"), ("--family", "codex")):
            with self.subTest(arguments=arguments):
                status, output, errors = self.cli("save", "--last-week", *arguments)
                self.assertEqual(status, 2, output + errors)
                self.assertIn("save --last-week", errors)


if __name__ == "__main__":
    _ = unittest.main()

