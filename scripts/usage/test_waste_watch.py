"""The waste watch over invented stores; send.py is always a stub and no real state is touched."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.usage import collect, usage_report, waste_watch
from scripts.usage.test_collect import FAILING_WATCH, CollectorFixture


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
    lookup: waste_watch.ShowrunnerLookup  # pyright: ignore[reportUninitializedInstanceVariable]
    lookups: int  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
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
        _ = showrunner_doc.write_text(
            "# Watch\n\n## Units\n\n| Unit | Worktree |\n| --- | --- |\n"
            + f"| alpha | `{self.worktree}` |\n", encoding="utf-8")
        self.lookup = waste_watch.ShowrunnersFound((waste_watch.Showrunner(
            "watch", ADDRESS, usage_report.document_units(showrunner_doc)),))
        self.lookups = 0

        def registered() -> waste_watch.ShowrunnerLookup:
            self.lookups += 1
            return self.lookup

        _ = self.enterContext(mock.patch.object(waste_watch, "registered_showrunners", registered))
        with closing(sqlite3.connect(self.db_path)) as db, db:
            _ = db.executescript(collect.SCHEMA)
            collect.migrate_store(db)

    def request(self, thread: str, minute: int, uncached: int, cached: int = 0, *, activity: str = "work",
                action: str | None = None, role: str | None = "impl", family: str = "codex",
                folder: str | None = None, hour: int = 11) -> None:
        """gpt-5.6-sol weighs cached input 0.1 and output 8; these requests have no output."""
        at = NOW.replace(hour=hour, minute=minute, second=0)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            _ = db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("natedev", family, "thread" if family == "codex" else "session", "", thread,
                            f"{thread}.jsonl:{hour * 60 + minute:05d}", at.isoformat(), "gpt-5.6-sol", None,
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
            "See more: python3 ~/.claude/scripts/usage/usage_report.py wait_share --by unit,role "
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
                    "watch", "", usage_report.document_units(self.root / "elsewhere/watch-production.md")),)),
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
                         family: str = "codex") -> None:
        for minute, size in enumerate(sizes):
            self.request(thread, minute, 1_000, size - 1_000, role=role, family=family)

    def test_five_requests_in_a_row_above_250000_flag_a_codex_worker(self) -> None:
        self.context_requests("stuck-worker", [100_000, 260_000, 270_000, 280_000, 290_000, 300_000, 120_000])
        output = self.run_watch(dry_run=True)
        self.assertEqual(output.splitlines(), [
            f"waste_watch dry run: would send to {ADDRESS}:",
            "Message from usage-waste-watch: alpha has 1 Codex worker thread whose context stayed above "
            + "250,000 tokens for 5 or more requests in a row in the last 2 hours",
            "Context above 250,000 tokens, so compaction is not happening:",
            "- impl codex k-worker: 5 requests in a row, peak 300,000 tokens",
            "See more: python3 ~/.claude/scripts/usage/usage_report.py wait_share --by unit,role "
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
        self.assertIn("- unassigned codex assigned: 5 requests in a row, peak 300,000 tokens", lines)
        self.assertEqual(len([line for line in lines if line.startswith("waste_watch dry run")]), 1)

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
