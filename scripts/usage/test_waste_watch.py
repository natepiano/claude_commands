"""The waste watch over invented stores; send.py is always a stub and no real state is touched."""

from __future__ import annotations

from scripts.shutdown import remote

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import AbstractContextManager, closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.usage import collect, usage_report, waste_watch
from scripts.production import session_staff, showrunners, staff
from scripts.production.staff_test_support import (
    fixture_unit_location, isolated_source_readers, isolated_staff_file, recorded_showrunner, source_environment,
    fixture_member,
)
from scripts.usage.test_collect import FAILING_WATCH, CollectorFixture


REAL_DISCOVERY = waste_watch.registered_showrunners

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
ADDRESS = "uds:/invented/showrunner.sock"
FAKE_SEND = """import json, os, sys
with open(os.environ["FAKE_SEND_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
print("FAILED: invented" if os.environ.get("FAKE_SEND_EXIT", "0") not in ("0", "1") else "SENT: invented")
sys.exit(int(os.environ.get("FAKE_SEND_EXIT", "0")))
"""


@final
class WasteWatchTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    db_path: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    docs: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    worktree: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    sends: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    models_cache: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    lookup: waste_watch.ShowrunnerLookup  # pyright: ignore[reportUninitializedInstanceVariable]
    lookups: int  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "CODEX_HOME": str(self.root / ".codex")}))
        self.models_cache = self.root / ".codex/models_cache.json"
        self.models_cache.parent.mkdir()
        _ = self.models_cache.write_text(json.dumps({"models": [
            {"slug": "gpt-5.6-sol", "context_window": 272_000, "effective_context_window_percent": 95},
            {"slug": "gpt-6.1-sol", "context_window": 500_000, "effective_context_window_percent": 95},
        ]}), encoding="utf-8")
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        state = self.root / "state"
        state.mkdir()
        self.db_path = state / "usage.db"
        self.sends = self.root / "sends.jsonl"
        send = self.root / "send.py"
        _ = send.write_text(FAKE_SEND, encoding="utf-8")
        self.enterContext(mock.patch.dict(os.environ, {
            "USAGE_STATE_DIR": str(state), "WASTE_WATCH_SEND": str(send), "FAKE_SEND_LOG": str(self.sends),
            "FAKE_SEND_EXIT": "0"}))
        self.docs = self.root / "production-docs"
        self.docs.mkdir()
        self.worktree = self.root / "worktrees/alpha"
        showrunner_doc = self.root / "elsewhere/watch-production.md"
        showrunner_doc.parent.mkdir()
        _ = showrunner_doc.write_text("# Watch\n", encoding="utf-8")
        environment = source_environment(self.root / "staff-sources")
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", environment))
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
        _ = self.enterContext(isolated_source_readers())
        owner = recorded_showrunner("watch", showrunner_doc)
        _ = fixture_unit_location(owner, "alpha", self.worktree)
        self.lookup = waste_watch.ShowrunnersFound((waste_watch.Showrunner(
            "watch", ADDRESS, usage_report.document_units(owner)),))
        self.lookups = 0

        def registered(*, resolver: session_staff.RowResolver | None = None) -> waste_watch.ShowrunnerLookup:
            del resolver
            self.lookups += 1
            return self.lookup

        _ = self.enterContext(mock.patch.object(waste_watch, "registered_showrunners", registered))
        with closing(sqlite3.connect(self.db_path)) as db, db:
            _ = db.executescript(collect.SCHEMA)
            collect.migrate_store(db)

    def request(self, thread: str, minute: int, uncached: int, cached: int = 0, *, activity: str = "work",
                action: str | None = None, role: str | None = "impl", family: str = "codex",
                folder: str | None = None, hour: int = 11, model: str = "gpt-5.6-sol") -> None:
        """gpt-5.6-sol weighs cached input 0.1 and output 8; these requests have no output."""
        at = NOW.replace(hour=hour, minute=0, second=0) + timedelta(minutes=minute)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            _ = db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("natedev", family, "thread" if family == "codex" else "session", "", thread,
                            f"{thread}.jsonl:{hour * 60 + minute:05d}", at.isoformat(), model, None,
                            uncached, 0, 0, cached, 0, 0, role, None, "recorded", "not_applicable",
                            activity, action))
            _ = db.execute("INSERT OR IGNORE INTO folders VALUES (?,?,?,?,?)",
                           ("natedev", family, "", thread, folder or str(self.worktree / "src")))

    def wait(self, thread: str, minute: int, uncached: int, action: str = "empty write_stdin", *,
             folder: str | None = None, hour: int = 11) -> None:
        self.request(thread, minute, uncached, activity="wait", action=action, folder=folder, hour=hour)

    def run_watch(self, *, now: datetime = NOW, dry_run: bool | None = None, db: Path | None = None) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            waste_watch.watch(db or self.db_path, self.docs, now=now, dry_run=dry_run)
        return output.getvalue()

    def sent(self) -> list[list[str]]:
        if not self.sends.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.sends.read_text(encoding="utf-8").splitlines()]

    def alert_times(self) -> dict[str, str]:
        path = self.db_path.parent / "waste_watch.json"
        return cast(dict[str, str], json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}

    def test_docless_row_warns_without_failing_healthy_showrunner_discovery(self) -> None:
        _ = fixture_member("showrunner", name="docless")
        errors = io.StringIO()
        with redirect_stderr(errors):
            found = REAL_DISCOVERY()
        self.assertIsInstance(found, waste_watch.ShowrunnersFound)
        assert isinstance(found, waste_watch.ShowrunnersFound)
        self.assertEqual([runner.name for runner in found.showrunners], ["watch"])
        self.assertEqual(errors.getvalue().splitlines(), ["warning: docless: the production document is not recorded"])

    def test_partial_discovery_keeps_the_lookup_unknown_instead_of_complete(self) -> None:
        partial = showrunners.ShowrunnerDiscovery([], (
            showrunners.ShowrunnerLookupFailure("missing", "fixture lookup failed"),))
        # The fixture normally replaces this wrapper; exercise the real library entry here.
        with (mock.patch.object(showrunners, "registered_showrunners", return_value=partial),
              mock.patch.object(staff, "snapshot", side_effect=OSError("fixture lookup failed"))):
            found = REAL_DISCOVERY()
        self.assertIsInstance(found, waste_watch.ShowrunnerLookupFailed)
        assert isinstance(found, waste_watch.ShowrunnerLookupFailed)
        self.assertIn("fixture lookup failed", found.reason)

    def test_wait_share_over_ten_percent_on_a_million_weighted_tokens_is_sent_to_the_showrunner(self) -> None:
        self.request("worker-one", 0, 900_000)
        self.wait("worker-one", 1, 150_000)
        self.wait("worker-two", 2, 50_000, action="sleep")
        self.wait("worker-two", 3, 1_000, action="sleep")
        self.assertEqual(self.run_watch(), "")
        sent = self.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][:5], ["--to", ADDRESS, "--from", "usage-waste-watch", "--text"])
        self.assertEqual(sent[0][5].splitlines(), [
            "Message from usage-waste-watch: alpha spent 18% of its weighted usage in the last 2 hours "
            + "on requests that only waited",
            "Window from 2026-10-16T10:00:00+00:00: 1,101,000 weighted tokens, 201,000 of them on waiting requests.",
            "Threads waiting most:",
            "- impl codex rker-one: 14% of 1,050,000 weighted",
            "- impl codex rker-two: 100% of 51,000 weighted",
            "Most common waiting action: sleep (2 requests).",
            "See more: ~/.claude/scripts/lib/py ~/.claude/scripts/usage/usage_report.py wait_share --by unit,role "
            + "--since 2026-10-16T10:00:00+00:00",
        ])
        self.assertEqual(self.alert_times(), {"alpha": NOW.isoformat()})

    def test_waiting_outside_codex_workers_never_alerts(self) -> None:
        # A Claude session's one read of a task's output after its notice is labelled wait.
        self.request("director", 0, 900_000, role="director", family="claude")
        self.request("director", 1, 600_000, activity="wait", action="cat *.output", role="director",
                     family="claude")
        self.request("showrunner", 2, 900_000, role="showrunner")
        self.request("showrunner", 3, 600_000, activity="wait", action="sleep", role="showrunner")
        self.request("worker", 4, 900_000)
        self.wait("worker", 5, 50_000)
        self.assertEqual(self.run_watch(dry_run=True), "")
        self.assertEqual(self.lookups, 0)

    def test_ten_percent_exactly_or_under_a_million_weighted_tokens_sends_nothing(self) -> None:
        self.request("worker", 0, 900_000)
        self.wait("worker", 1, 100_000)
        self.assertEqual(self.run_watch(), "")
        self.request("small", 2, 400_000, folder="/invented/small")
        self.wait("small", 3, 400_000, folder="/invented/small")
        self.assertEqual(self.run_watch(), "")
        self.assertEqual((self.sent(), self.alert_times()), ([], {}))

    def test_only_requests_in_the_last_two_hours_count(self) -> None:
        self.request("worker", 0, 900_000)
        self.wait("worker", 59, 500_000, hour=9)
        self.assertEqual(self.run_watch(), "")
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.lookups, 0)

    def test_an_empty_window_looks_up_no_showrunner(self) -> None:
        self.assertEqual(self.run_watch(), "")
        self.assertEqual((self.lookups, self.sent()), (0, []))

    def test_a_unit_is_alerted_at_most_once_in_six_hours(self) -> None:
        self.request("worker", 0, 900_000)
        self.wait("worker", 1, 500_000)
        _ = self.run_watch()
        _ = self.run_watch(now=NOW + timedelta(hours=1))
        self.request("worker", 0, 900_000, hour=17)
        self.wait("worker", 1, 500_000, hour=17)
        _ = self.run_watch(now=NOW + timedelta(hours=5, minutes=59))
        self.assertEqual(len(self.sent()), 1)
        later = NOW + timedelta(hours=6)
        _ = self.run_watch(now=later)
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(self.alert_times(), {"alpha": later.isoformat()})

    def test_dry_run_prints_the_destination_sends_nothing_writes_nothing_and_ignores_the_six_hours(self) -> None:
        self.request("worker", 0, 900_000)
        self.wait("worker", 1, 500_000)
        state = self.db_path.parent / "waste_watch.json"
        _ = state.write_text(json.dumps({"alpha": NOW.isoformat()}), encoding="utf-8")
        before = state.read_bytes()
        output = self.run_watch(dry_run=True)
        self.assertTrue(output.startswith(f"waste_watch dry run: would send to {ADDRESS}:\n"
                                          + "Message from usage-waste-watch: alpha spent 36%"), output)
        self.assertEqual((self.sent(), state.read_bytes()), ([], before))

    def test_a_store_other_than_the_default_prints_instead_of_sending(self) -> None:
        self.request("worker", 0, 900_000)
        self.wait("worker", 1, 500_000)
        other = self.root / "scratch.db"
        _ = other.write_bytes(self.db_path.read_bytes())
        output = self.run_watch(db=other)
        self.assertIn(f"waste_watch dry run: would send to {ADDRESS}:\n", output)
        self.assertEqual((self.sent(), self.alert_times()), ([], {}))

    def test_without_a_showrunner_the_alert_goes_to_stdout_and_counts_as_given(self) -> None:
        doc = self.root / "loose-production.md"
        _ = doc.write_text("# Loose production\n")
        owner = recorded_showrunner("loose-owner", doc)
        _ = fixture_member("unit", name="loose", parent=owner.id,
                           worktree=staff.PresentWorktree(remote.local_machine(), Path("/invented/loose")))
        self.request("loose", 0, 900_000, folder="/invented/loose")
        self.wait("loose", 1, 500_000, folder="/invented/loose")
        output = self.run_watch()
        self.assertTrue(output.startswith("waste_watch: no registered showrunner lists unit loose; "
                                          + "alert for the journal:\nMessage from usage-waste-watch: loose spent"),
                        output)
        self.assertEqual((self.sent(), list(self.alert_times())), ([], ["loose"]))

    def test_a_stopped_showrunner_or_a_failed_lookup_prints_the_reason(self) -> None:
        self.request("worker", 0, 900_000)
        self.wait("worker", 1, 500_000)
        for lookup, reason in (
                (waste_watch.ShowrunnersFound((waste_watch.Showrunner(
                    "watch", "", usage_report.document_units(showrunners.owner_for("watch"))),)),
                 "showrunner watch is not running"),
                (waste_watch.ShowrunnerLookupFailed("showrunner lookup failed: invented"),
                 "showrunner lookup failed: invented")):
            with self.subTest(reason=reason):
                self.lookup = lookup
                self.assertTrue(self.run_watch(dry_run=True).startswith(
                    f"waste_watch dry run: would send to stdout ({reason}):\n"))
        self.assertEqual(self.sent(), [])

    def test_a_failed_send_is_printed_and_not_recorded_and_a_queued_one_is_recorded(self) -> None:
        self.request("worker", 0, 900_000)
        self.wait("worker", 1, 500_000)
        os.environ["FAKE_SEND_EXIT"] = "3"
        output = self.run_watch()
        self.assertTrue(output.startswith("waste_watch: could not tell watch: send.py exited 3: FAILED: invented; "
                                          + "alert for the journal:\n"), output)
        self.assertEqual(self.alert_times(), {})
        os.environ["FAKE_SEND_EXIT"] = "1"
        self.assertEqual(self.run_watch(), "")
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(list(self.alert_times()), ["alpha"])

    def context_requests(self, thread: str, sizes: list[int], *, role: str | None = "impl",
                         family: str = "codex", model: str = "gpt-5.6-sol") -> None:
        for minute, size in enumerate(sizes):
            self.request(thread, minute, 1_000, size - 1_000, role=role, family=family, model=model, hour=10)

    def test_five_requests_above_the_model_limit_flag_a_codex_worker(self) -> None:
        self.context_requests("stuck-worker", [100_000, 260_000, 270_000, 280_000, 290_000, 300_000, 120_000])
        output = self.run_watch(dry_run=True)
        self.assertEqual(output.splitlines(), [
            f"waste_watch dry run: would send to {ADDRESS}:",
            "Message from usage-waste-watch: alpha has 1 Codex worker thread above the model compaction limit.",
            "Context exceeded the model compaction limit for 5 or more consecutive requests in the last 2 hours:",
            "- impl codex k-worker: limits gpt-5.6-sol: 244,800 tokens; 5 consecutive requests; peak 300,000 tokens.",
            "See more: ~/.claude/scripts/lib/py ~/.claude/scripts/usage/usage_report.py wait_share --by unit,role "
            + "--since 2026-10-16T10:00:00+00:00",
        ])

    def test_four_in_a_row_another_role_or_a_claude_session_is_not_flagged(self) -> None:
        self.context_requests("four", [260_000, 270_000, 280_000, 290_000, 100_000, 260_000, 260_000])
        self.context_requests("director", [300_000] * 6, role="director")
        self.context_requests("claude", [300_000] * 6, family="claude")
        self.assertEqual(self.run_watch(dry_run=True), "")
        self.assertEqual(self.lookups, 0)

    def test_an_unassigned_codex_thread_is_flagged_and_joins_the_wait_alert(self) -> None:
        self.request("worker", 0, 900_000)
        self.wait("worker", 1, 500_000)
        self.context_requests("unassigned", [300_000] * 5, role=None)
        lines = self.run_watch(dry_run=True).splitlines()
        self.assertIn("Message from usage-waste-watch: alpha spent", lines[1])
        self.assertIn("- unassigned codex assigned: limits gpt-5.6-sol: 244,800 tokens; "
                      + "5 consecutive requests; peak 300,000 tokens.", lines)
        self.assertEqual(len([line for line in lines if line.startswith("waste_watch dry run")]), 1)

    def test_sol_61_normal_growth_does_not_alert(self) -> None:
        self.context_requests("5abb0b88", [343_068] * 61, model="gpt-6.1-sol")
        self.assertEqual(waste_watch.model_context_limits()["gpt-6.1-sol"], 450_000)
        self.assertEqual(self.run_watch(dry_run=True), "")
        self.assertEqual(self.lookups, 0)

    def test_each_thread_names_its_model_limit(self) -> None:
        self.context_requests("small", [300_000] * 5)
        self.context_requests("large", [460_000] * 5, model="gpt-6.1-sol")
        output = self.run_watch(dry_run=True)
        self.assertIn("alpha has 2 Codex worker threads", output)
        self.assertIn("small: limits gpt-5.6-sol: 244,800 tokens", output)
        self.assertIn("large: limits gpt-6.1-sol: 450,000 tokens", output)

    def test_the_limit_itself_interrupts_a_run(self) -> None:
        self.context_requests("boundary", [244_801] * 4 + [244_800] + [244_801] * 4)
        self.assertEqual(self.run_watch(dry_run=True), "")
        self.context_requests("above", [244_801] * 5)
        self.assertIn("above: limits gpt-5.6-sol: 244,800 tokens", self.run_watch(dry_run=True))

    def test_each_request_uses_its_model_and_unknown_models_interrupt_a_run(self) -> None:
        for thread, model in (("changed", "gpt-6.1-sol"), ("unknown", "unknown-model")):
            for minute in range(9):
                self.request(thread, minute, 1_000, 299_000,
                             model=model if minute == 4 else "gpt-5.6-sol")
        self.assertEqual(self.run_watch(dry_run=True), "")
        self.assertEqual(self.lookups, 0)

    def test_one_run_can_report_limits_for_two_models(self) -> None:
        for minute in range(10):
            self.request("changed", minute, 1_000, 459_000,
                         model="gpt-5.6-sol" if minute < 5 else "gpt-6.1-sol")
        output = self.run_watch(dry_run=True)
        self.assertIn("alpha has 1 Codex worker thread", output)
        self.assertIn("changed: limits gpt-5.6-sol: 244,800 tokens, gpt-6.1-sol: 450,000 tokens; "
                      + "10 consecutive requests", output)

    def test_model_changes_keep_a_run_when_both_models_exceed_their_limits(self) -> None:
        for minute in range(9):
            self.request("changed", minute, 1_000, 459_000,
                         model="gpt-6.1-sol" if minute == 4 else "gpt-5.6-sol")
        output = self.run_watch(dry_run=True)
        self.assertIn("9 consecutive requests", output)
        self.assertIn("gpt-5.6-sol: 244,800 tokens, gpt-6.1-sol: 450,000 tokens", output)

    def test_cache_limits_follow_codex_defaults_and_clamping(self) -> None:
        models: list[dict[str, object]] = [
            {"slug": "default", "context_window": 500_001, "effective_context_window_percent": 80},
            {"slug": "lower", "context_window": 500_000, "auto_compact_token_limit": 200_000},
            {"slug": "higher", "context_window": 500_000, "auto_compact_token_limit": 490_000},
            {"slug": "maximum", "context_window": None, "max_context_window": 400_000},
            {"slug": "null", "context_window": 500_000, "auto_compact_token_limit": None},
            {"slug": "no-window"},
            {"slug": "bad-window", "context_window": "500000"},
            {"slug": "bad-limit", "context_window": 500_000, "auto_compact_token_limit": "200000"},
            {"slug": "boolean-window", "context_window": True},
            {"slug": "boolean-limit", "context_window": 500_000, "auto_compact_token_limit": True},
        ]
        _ = self.models_cache.write_text(json.dumps({"models": models}), encoding="utf-8")
        self.assertEqual(waste_watch.model_context_limits(), {
            "default": 450_000, "lower": 200_000, "higher": 450_000, "maximum": 360_000, "null": 450_000})

    def test_unreadable_cache_values_skip_context_alerts_without_output(self) -> None:
        self.context_requests("unknown", [900_000] * 5)
        for document in ("", "{", "[]", '{"models": null}', '{"models": [null, {}]}',
                         '{"models": [{"slug": "gpt-5.6-sol", "context_window": "bad"}]}'):
            with self.subTest(document=document):
                _ = self.models_cache.write_text(document, encoding="utf-8")
                errors = io.StringIO()
                with redirect_stderr(errors):
                    self.assertEqual(self.run_watch(dry_run=True), "")
                self.assertEqual(errors.getvalue(), "")
        self.models_cache.unlink()
        self.assertEqual(self.run_watch(dry_run=True), "")

    def test_an_unreadable_model_does_not_disable_other_models(self) -> None:
        _ = self.models_cache.write_text(json.dumps({"models": [
            {"slug": "gpt-5.6-sol", "context_window": 272_000},
            {"slug": "gpt-6.1-sol"},
        ]}), encoding="utf-8")
        self.context_requests("known", [300_000] * 5)
        self.context_requests("unknown", [900_000] * 5, model="gpt-6.1-sol")
        output = self.run_watch(dry_run=True)
        self.assertIn("alpha has 1 Codex worker thread", output)
        self.assertIn("known: limits gpt-5.6-sol: 244,800 tokens", output)
        self.assertNotIn("gpt-6.1-sol", output)

    def test_wait_alert_still_works_without_a_models_cache(self) -> None:
        self.models_cache.unlink()
        self.request("worker", 0, 900_000)
        self.wait("worker", 1, 500_000)
        self.assertIn("alpha spent 36%", self.run_watch(dry_run=True))

    def test_cache_defaults_to_the_patched_home(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            self.assertEqual(waste_watch.model_context_limits()["gpt-6.1-sol"], 450_000)

    def test_the_command_line_dry_run_names_its_store_and_time(self) -> None:
        self.request("worker", 0, 900_000)
        self.wait("worker", 1, 500_000)
        output = io.StringIO()
        with redirect_stdout(output):
            status = waste_watch.main(["--db", str(self.db_path), "--production-docs", str(self.docs),
                                       "--dry-run", "--now", "2026-10-16T12:00:00Z"])
        self.assertEqual(status, 0)
        self.assertTrue(output.getvalue().startswith(f"waste_watch dry run: would send to {ADDRESS}:\n"))
        self.assertEqual(self.sent(), [])


@final
class CollectorWatchTests(CollectorFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root)}))
        environment = source_environment(self.root / "staff-sources")
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", environment))
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
        _ = self.enterContext(isolated_source_readers())
        _ = (self.root / "staff.json").write_text('{"schema": 2, "rows": []}\n')

    def test_a_good_pass_runs_the_watch_with_its_store_and_production_docs(self) -> None:
        result = self.cli_with_watch('import sys\nprint("watched", *sys.argv[1:])\n'
                                     + 'print("watch note", file=sys.stderr)\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"watched --db {self.db_path} --production-docs {self.root / 'production-docs'}\n",
                      result.stdout)
        self.assertEqual(result.stderr, "watch note\n")

    def test_a_watch_failure_prints_one_line_and_the_pass_still_succeeds(self) -> None:
        result = self.cli_with_watch(FAILING_WATCH)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "waste_watch: exit 1: fixture watch failure second line\n")
        self.assertGreater(len(self.rows("SELECT * FROM turns")), 0)


if __name__ == "__main__":
    _ = unittest.main()
