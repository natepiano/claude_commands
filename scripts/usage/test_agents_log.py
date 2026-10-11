"""Usage assignments survive concurrency, bad input, and historical backfill."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.usage import agents_log


REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).with_name("agents_log.py")
AT = datetime(2026, 10, 11, 17, 30, tzinfo=timezone.utc)


class AgentsLogTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    state: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    log_path: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.log_path = self.state / "agents.jsonl"
        self.enterContext(patch.dict(os.environ, {"USAGE_STATE_DIR": str(self.state)}))

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=REPOSITORY,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def records(self) -> list[dict[str, object]]:
        return [cast("dict[str, object]", json.loads(line))
                for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def backfill_fixture(self) -> tuple[Path, Path, datetime, datetime]:
        archive = self.root / "archive"
        archived_run = archive / "archived-run"
        archived_run.mkdir(parents=True)
        roster = archived_run / "mesh_roster.json"
        _ = roster.write_text(json.dumps({
            "project-test": {
                "thread_id": "archived-thread", "role": "review",
                "previous_role": "impl", "status": "done",
            },
        }), encoding="utf-8")
        roster_stamp = 1_700_000_000
        os.utime(roster, (roster_stamp, roster_stamp))
        _ = (archived_run / "review_status_adversary").write_text("reviewed\n", encoding="utf-8")
        _ = (archived_run / "review_agent_adversary.log").write_text(
            "Review seat project-test starts\n", encoding="utf-8")
        seats = archived_run / "seats"
        _ = seats.write_text("claude-one\tworker-one\nclaude-two\tworker-two\n", encoding="utf-8")
        ledger_stamp = roster_stamp + 100
        os.utime(seats, (ledger_stamp, ledger_stamp))

        live = self.root / "live"
        live_run = live / "live-run"
        live_run.mkdir(parents=True)
        _ = (live_run / "mesh_roster.json").write_text(json.dumps({
            "project-impl": {"thread_id": "live-thread", "role": "test", "status": "done"},
        }), encoding="utf-8")
        active = live / "active"
        active.mkdir()
        _ = (active / "director-session").write_text(f"{live_run}\n", encoding="utf-8")
        # active/ is metadata, never another run to scan.
        _ = (active / "mesh_roster.json").write_text(json.dumps({
            "fake": {"thread_id": "not-a-run", "role": "impl"},
        }), encoding="utf-8")
        return (
            archive, live,
            datetime.fromtimestamp(roster_stamp, timezone.utc),
            datetime.fromtimestamp(ledger_stamp, timezone.utc),
        )

    def test_state_directory_uses_the_environment_override(self) -> None:
        self.assertEqual(agents_log.state_dir(), self.state)
        with patch.dict(os.environ):
            _ = os.environ.pop("USAGE_STATE_DIR")
            with patch.object(Path, "home", return_value=self.root):
                self.assertEqual(agents_log.state_dir(), self.root / ".local/state/usage-model")

    def test_append_and_read_back_an_assignment(self) -> None:
        agents_log.append("thread-1", "impl", None, AT)

        record = self.records()[0]
        self.assertEqual(set(record), {"id", "role", "at"})
        self.assertEqual((record["id"], record["role"]), ("thread-1", "impl"))
        self.assertEqual(datetime.fromisoformat(cast("str", record["at"])), AT)
        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.problems, ())
        self.assertEqual(log.assignments, {
            "thread-1": (agents_log.Assignment(AT, "impl", None, False),),
        })

    def test_append_normalizes_to_utc_seconds_and_includes_given_metadata(self) -> None:
        local_at = datetime(2026, 10, 11, 13, 30, 5, 987654,
                            tzinfo=timezone(timedelta(hours=-4)))
        agents_log.append("review-thread", "review", "contract", local_at, approximate=True)

        record = self.records()[0]
        self.assertEqual(set(record), {"id", "role", "lens", "at", "approximate"})
        recorded_at = datetime.fromisoformat(cast("str", record["at"]))
        self.assertEqual(recorded_at, AT + timedelta(seconds=5))
        self.assertEqual(recorded_at.microsecond, 0)
        self.assertEqual(recorded_at.utcoffset(), timedelta(0))
        self.assertEqual(record["lens"], "contract")
        self.assertIs(record["approximate"], True)
        self.assertEqual(agents_log.assignments(self.log_path).assignments["review-thread"], (
            agents_log.Assignment(AT + timedelta(seconds=5), "review", "contract", True),
        ))

    def test_cli_append_accepts_every_role_and_lens(self) -> None:
        for role in ("showrunner", "director", "impl", "test", "fix", "review", "worker"):
            with self.subTest(role=role):
                result = self.cli("append", "--id", role, "--role", role)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
        for lens in ("adversary", "contract", "craft", "ux"):
            with self.subTest(lens=lens):
                result = self.cli("append", "--id", lens, "--role", "review", "--lens", lens)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
        records = self.records()
        self.assertEqual(len(records), 11)
        self.assertEqual({record["role"] for record in records[:7]},
                         {"showrunner", "director", "impl", "test", "fix", "review", "worker"})
        self.assertEqual({record["lens"] for record in records[7:]},
                         {"adversary", "contract", "craft", "ux"})
        for record in records:
            recorded_at = datetime.fromisoformat(cast("str", record["at"]))
            self.assertEqual(recorded_at.microsecond, 0)
            self.assertEqual(recorded_at.utcoffset(), timedelta(0))
            self.assertNotIn("approximate", record)

    def test_cli_rejects_an_unknown_role_or_lens_without_writing(self) -> None:
        for arguments in (("--role", "unknown"),
                          ("--role", "review", "--lens", "unknown")):
            with self.subTest(arguments=arguments):
                result = self.cli("append", "--id", "thread-1", *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage:", result.stderr)
                self.assertFalse(self.log_path.exists())

    def test_assignments_are_grouped_and_sorted_by_time(self) -> None:
        agents_log.append("thread-1", "review", "craft", AT + timedelta(hours=2))
        agents_log.append("thread-2", "worker", None, AT + timedelta(hours=1))
        agents_log.append("thread-1", "impl", None, AT)

        log = agents_log.assignments(self.log_path)

        self.assertEqual(log.problems, ())
        self.assertEqual(log.assignments["thread-1"], (
            agents_log.Assignment(AT, "impl", None, False),
            agents_log.Assignment(AT + timedelta(hours=2), "review", "craft", False),
        ))
        self.assertIsInstance(log.assignments["thread-2"], tuple)

    def test_malformed_lines_are_counted_and_valid_assignments_survive(self) -> None:
        agents_log.append("thread-1", "impl", None, AT)
        malformed = [
            "not json", "[]", "{}",
            json.dumps({"id": "bad-time", "role": "impl", "at": "not-a-date"}),
            json.dumps({"id": "bad-role", "role": "unknown", "at": AT.isoformat()}),
            json.dumps({"id": "bad-lens", "role": "review", "lens": "unknown", "at": AT.isoformat()}),
        ]
        with self.log_path.open("a", encoding="utf-8") as handle:
            _ = handle.write("\n".join(malformed) + "\n")
        agents_log.append("thread-1", "test", None, AT + timedelta(minutes=1))

        log = agents_log.assignments(self.log_path)

        self.assertIsInstance(log.problems, tuple)
        self.assertEqual(len(log.problems), len(malformed))
        self.assertEqual(set(log.assignments), {"thread-1"})
        self.assertEqual(len(log.assignments["thread-1"]), 2)

    def test_deeply_nested_json_is_one_problem_and_later_assignments_load(self) -> None:
        agents_log.append("thread-1", "impl", None, AT)
        with self.log_path.open("a", encoding="utf-8") as handle:
            _ = handle.write("[" * 2000 + "0" + "]" * 2000 + "\n")
        agents_log.append("thread-1", "review", "contract", AT + timedelta(minutes=1))

        log = agents_log.assignments(self.log_path)

        self.assertEqual(len(log.problems), 1)
        self.assertIn(f"{self.log_path}:2:", log.problems[0])
        self.assertEqual([assignment.role for assignment in log.assignments["thread-1"]],
                         ["impl", "review"])

    def test_append_separates_a_truncated_tail_from_new_assignments(self) -> None:
        agents_log.append("earlier-thread", "impl", None, AT)
        with self.log_path.open("a", encoding="utf-8") as handle:
            _ = handle.write('{"id":"broken')

        agents_log.append("later-thread-é", "review", "contract", AT + timedelta(minutes=1))
        agents_log.append("last-thread", "test", None, AT + timedelta(minutes=2))

        log = agents_log.assignments(self.log_path)
        self.assertEqual(len(log.problems), 1)
        self.assertEqual(set(log.assignments), {"earlier-thread", "later-thread-é", "last-thread"})
        self.assertEqual(len(self.log_path.read_text(encoding="utf-8").splitlines()), 4)

    def test_role_at_selects_before_between_exactly_and_after_assignments(self) -> None:
        agents_log.append("thread-1", "review", "adversary", AT + timedelta(hours=1), approximate=True)
        agents_log.append("thread-1", "impl", None, AT)
        log = agents_log.assignments(self.log_path)

        self.assertEqual(agents_log.role_at(log, "thread-1", AT - timedelta(seconds=1)),
                         agents_log.Unassigned())
        self.assertEqual(agents_log.role_at(log, "absent", AT + timedelta(days=1)),
                         agents_log.Unassigned())
        for query in (AT, AT + timedelta(minutes=30)):
            with self.subTest(query=query):
                self.assertEqual(agents_log.role_at(log, "thread-1", query),
                                 agents_log.Assignment(AT, "impl", None, False))
        for query in (AT + timedelta(hours=1), AT + timedelta(days=1)):
            with self.subTest(query=query):
                self.assertEqual(agents_log.role_at(log, "thread-1", query),
                                 agents_log.Assignment(AT + timedelta(hours=1), "review", "adversary", True))

    def test_assignment_and_unassigned_are_frozen(self) -> None:
        assignment = agents_log.Assignment(AT, "impl", None, False)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(assignment, "role", "fix")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(agents_log.Unassigned(), "role", "impl")

    def test_concurrent_processes_append_complete_json_lines(self) -> None:
        script = """
import sys
from datetime import datetime, timezone
from scripts.usage.agents_log import append
sys.stdin.readline()
for index in range(20):
    append(f'{sys.argv[1]}-{index}-' + 'x' * 32768, 'worker', None,
           datetime.now(timezone.utc))
"""
        processes: list[subprocess.Popen[str]] = []
        try:
            for worker in range(4):
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", script, str(worker)],
                    cwd=REPOSITORY, env=os.environ.copy(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True,
                ))
            # Release every writer before waiting for any of them.
            for process in processes:
                assert process.stdin is not None
                _ = process.stdin.write("go\n")
                process.stdin.flush()
            for process in processes:
                output, errors = process.communicate(timeout=20)
                self.assertEqual((process.returncode, output, errors), (0, "", ""))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                _ = process.communicate()

        records = self.records()
        self.assertEqual(len(records), 80)
        expected_ids = {f"{worker}-{index}-" + "x" * 32768
                        for worker in range(4) for index in range(20)}
        self.assertEqual({record["id"] for record in records}, expected_ids)
        self.assertTrue(all(record["role"] == "worker" for record in records))
        self.assertEqual(agents_log.assignments(self.log_path).problems, ())

    def test_backfill_recovers_previous_role_workers_director_and_review_lens(self) -> None:
        archive, live, roster_at, ledger_at = self.backfill_fixture()

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.problems, ())
        self.assertEqual(set(log.assignments), {
            "archived-thread", "live-thread", "claude-one", "claude-two", "director-session",
        })
        previous, current = log.assignments["archived-thread"]
        self.assertEqual((previous.role, previous.lens), ("impl", None))
        self.assertEqual((current.role, current.lens, current.at), ("review", "adversary", roster_at))
        self.assertLess(previous.at, current.at)
        self.assertEqual(agents_log.role_at(log, "archived-thread", roster_at), current)
        for worker in ("claude-one", "claude-two"):
            self.assertEqual(log.assignments[worker], (
                agents_log.Assignment(ledger_at, "worker", None, True),
            ))
        self.assertEqual(log.assignments["director-session"][0].role, "director")
        self.assertEqual(log.assignments["live-thread"][0].role, "test")
        self.assertTrue(all(assignment.approximate for assignments in log.assignments.values()
                            for assignment in assignments))
        self.assertTrue(all(record["approximate"] is True for record in self.records()))

    def test_backfill_rerun_adds_nothing_even_if_mtime_changes(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        arguments = ("backfill", "--archive", str(archive), "--live", str(live))
        first = self.cli(*arguments)
        self.assertEqual((first.returncode, first.stderr), (0, ""))
        before = self.log_path.read_bytes()
        roster = archive / "archived-run/mesh_roster.json"
        os.utime(roster, (1_700_001_000, 1_700_001_000))

        second = self.cli(*arguments)

        self.assertEqual((second.returncode, second.stderr), (0, ""))
        self.assertEqual(self.log_path.read_bytes(), before)

    def test_backfill_preserves_exact_history_despite_a_newer_roster(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        agents_log.append("archived-thread", "review", "adversary", AT)
        roster = archive / "archived-run/mesh_roster.json"
        rewritten_at = (AT + timedelta(minutes=1)).timestamp()
        os.utime(roster, (rewritten_at, rewritten_at))

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        exact = agents_log.Assignment(AT, "review", "adversary", False)
        self.assertEqual(log.assignments["archived-thread"], (exact,))
        self.assertEqual(agents_log.role_at(log, "archived-thread", AT + timedelta(minutes=2)), exact)
        before = self.log_path.read_bytes()
        rerun = self.cli("backfill", "--archive", str(archive), "--live", str(live))
        self.assertEqual((rerun.returncode, rerun.stderr), (0, ""))
        self.assertEqual(self.log_path.read_bytes(), before)

    def test_backfill_skips_worker_and_director_ids_with_exact_history(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        for agent_id in ("claude-one", "director-session"):
            agents_log.append(agent_id, "showrunner", None, AT)

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        for agent_id in ("claude-one", "director-session"):
            self.assertEqual(log.assignments[agent_id], (
                agents_log.Assignment(AT, "showrunner", None, False),
            ))

    def test_backfill_fills_missing_roles_for_ids_with_only_approximate_history(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        agents_log.append("archived-thread", "review", "adversary", AT, approximate=True)

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        entries = log.assignments["archived-thread"]
        self.assertEqual([assignment.role for assignment in entries], ["impl", "review"])
        self.assertTrue(all(assignment.approximate for assignment in entries))
        self.assertEqual(entries[1].at, AT)

    def test_backfill_recovers_swapped_review_lenses_from_logs_naming_each_seat(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        run = archive / "archived-run"
        _ = (run / "mesh_roster.json").write_text(json.dumps({
            "project-test": {"thread_id": "contract-thread", "role": "review"},
            "project-impl": {"thread_id": "adversary-thread", "role": "review"},
        }), encoding="utf-8")
        _ = (run / "review_status_contract").write_text("reviewed\n", encoding="utf-8")
        _ = (run / "review_agent_adversary.log").write_text(
            "Review seat project-impl starts\n", encoding="utf-8")
        _ = (run / "review_agent_1_contract.log").write_text(
            "Review seat project-test starts\n", encoding="utf-8")

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.assignments["contract-thread"][0].lens, "contract")
        self.assertEqual(log.assignments["adversary-thread"][0].lens, "adversary")

    def test_backfill_uses_the_newest_review_log_that_names_the_full_seat(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        run = archive / "archived-run"
        old_log = run / "review_agent_adversary.log"
        newest_matching = run / "review_agent_2_contract.log"
        _ = newest_matching.write_text("seat=project-test; role=review\n", encoding="utf-8")
        unrelated = run / "review_agent_3_ux.log"
        _ = unrelated.write_text("seats: project-test2 other-project-test\n", encoding="utf-8")
        for index, path in enumerate((old_log, newest_matching, unrelated)):
            stamp = 1_700_000_000 + index * 100
            os.utime(path, (stamp, stamp))

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(agents_log.assignments(self.log_path).assignments["archived-thread"][-1].lens,
                         "contract")

    def test_backfill_leaves_review_lens_absent_without_a_log_naming_the_seat(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        run = archive / "archived-run"
        _ = (run / "review_agent_adversary.log").write_text(
            "Review seat another-test starts\n", encoding="utf-8")

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        self.assertIsNone(log.assignments["archived-thread"][-1].lens)
        review_rows = [row for row in self.records() if row["role"] == "review"]
        self.assertTrue(all("lens" not in row for row in review_rows))

    def test_backfill_recovers_workers_when_the_same_runs_roster_is_truncated(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        roster = archive / "archived-run/mesh_roster.json"
        _ = roster.write_text('{"project-test":', encoding="utf-8")

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn(str(roster), result.stderr)
        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.problems, ())
        self.assertNotIn("archived-thread", log.assignments)
        self.assertEqual(log.assignments["claude-one"][0].role, "worker")
        self.assertEqual(log.assignments["claude-two"][0].role, "worker")

    def test_backfill_recovers_roster_when_the_same_runs_seats_ledger_is_unreadable(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        seats = archive / "archived-run/seats"
        _ = seats.write_bytes(b"\xff")

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn(str(seats), result.stderr)
        log = agents_log.assignments(self.log_path)
        self.assertEqual(len(log.assignments["archived-thread"]), 2)
        self.assertNotIn("claude-one", log.assignments)

    def test_backfill_separates_a_truncated_tail_and_remains_idempotent(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        agents_log.append("exact-thread", "impl", None, AT)
        with self.log_path.open("a", encoding="utf-8") as handle:
            _ = handle.write('{"id":"broken')

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(len(log.problems), 1)
        self.assertEqual(set(log.assignments), {
            "exact-thread", "archived-thread", "live-thread", "claude-one", "claude-two", "director-session",
        })
        self.assertEqual(len(log.assignments["archived-thread"]), 2)
        before = self.log_path.read_bytes()
        rerun = self.cli("backfill", "--archive", str(archive), "--live", str(live))
        self.assertEqual((rerun.returncode, rerun.stderr), (0, ""))
        self.assertEqual(self.log_path.read_bytes(), before)

    def test_show_prints_a_markdown_table_and_filters_by_id(self) -> None:
        agents_log.append("visible-thread", "review", "ux", AT, approximate=True)
        agents_log.append("other-thread", "impl", None, AT + timedelta(hours=1))

        shown = self.cli("show")
        filtered = self.cli("show", "--id", "visible-thread")

        for result in (shown, filtered):
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            self.assertGreaterEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("|"), result.stdout)
            self.assertIn("---", lines[1])
            self.assertIn("visible-thread", result.stdout)
            self.assertIn("review", result.stdout)
            self.assertIn("ux", result.stdout)
        self.assertIn("other-thread", shown.stdout)
        self.assertNotIn("other-thread", filtered.stdout)

    def test_cli_write_failure_reports_one_line_and_exits_successfully(self) -> None:
        self.state.mkdir()
        self.log_path.mkdir()

        result = self.cli("append", "--id", "thread-1", "--role", "impl")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith("agents_log: "), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_directory_creation_failure_also_exits_successfully(self) -> None:
        _ = self.state.write_text("a file cannot contain agents.jsonl", encoding="utf-8")

        result = self.cli("append", "--id", "thread-1", "--role", "worker")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith("agents_log: "), result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
