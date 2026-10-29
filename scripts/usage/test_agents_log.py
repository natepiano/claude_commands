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


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

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
            timeout=TEST_HANG_GUARD_SECS,
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
            "Review worker project-test starts\n", encoding="utf-8")
        workers = archived_run / "workers"
        _ = workers.write_text("claude-one\tworker-one\nclaude-two\tworker-two\n", encoding="utf-8")
        ledger_stamp = roster_stamp + 100
        os.utime(workers, (ledger_stamp, ledger_stamp))

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
        for role in ("showrunner", "director", "impl", "test", "fix", "review", "worker",
                     "interactive", "subagent"):
            with self.subTest(role=role):
                result = self.cli("append", "--id", role, "--role", role)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
        for lens in ("adversary", "contract", "craft", "ux"):
            with self.subTest(lens=lens):
                result = self.cli("append", "--id", lens, "--role", "review", "--lens", lens)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
        records = self.records()
        self.assertEqual(len(records), 13)
        self.assertEqual({record["role"] for record in records[:9]},
                         {"showrunner", "director", "impl", "test", "fix", "review", "worker",
                          "interactive", "subagent"})
        self.assertEqual({record["lens"] for record in records[9:]},
                         {"adversary", "contract", "craft", "ux"})
        for record in records:
            recorded_at = datetime.fromisoformat(cast("str", record["at"]))
            self.assertEqual(recorded_at.microsecond, 0)
            self.assertEqual(recorded_at.utcoffset(), timedelta(0))
            self.assertNotIn("approximate", record)

    def test_interactive_and_subagent_roles_survive_append_reader_and_show(self) -> None:
        roles: tuple[agents_log.Role, ...] = ("interactive", "subagent")
        for role in roles:
            agents_log.append(f"{role}-id", role, None, AT)

        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.problems, ())
        for role in roles:
            self.assertEqual(log.assignments[f"{role}-id"], (
                agents_log.Assignment(AT, role, None, False),
            ))
        shown = self.cli("show")
        self.assertEqual((shown.returncode, shown.stderr), (0, ""))
        for role in roles:
            self.assertIn(f"| {role}-id | {role} |", shown.stdout)

    def test_register_default_returns_whether_it_wrote_and_keeps_the_original_assignment(self) -> None:
        self.assertIs(agents_log.register_default("session-one", "interactive", AT), True)
        before = self.log_path.read_bytes()

        self.assertIs(agents_log.register_default("session-one", "subagent", AT + timedelta(hours=1)), False)

        self.assertEqual(self.log_path.read_bytes(), before)
        record = self.records()[0]
        self.assertEqual(set(record), {"id", "role", "at"})
        self.assertEqual((record["id"], record["role"]), ("session-one", "interactive"))
        self.assertEqual(datetime.fromisoformat(cast(str, record["at"])), AT)

    def test_register_default_preserves_an_existing_assignment_in_every_role(self) -> None:
        roles: tuple[agents_log.Role, ...] = (
            "showrunner", "director", "impl", "test", "fix", "review", "worker",
            "interactive", "subagent",
        )
        for role in roles:
            agents_log.append(f"already-{role}", role, None, AT)
        before = self.log_path.read_bytes()

        for role in roles:
            with self.subTest(role=role):
                self.assertIs(agents_log.register_default(f"already-{role}", "interactive", AT), False)

        self.assertEqual(self.log_path.read_bytes(), before)

    def test_register_default_recognizes_eight_hex_character_worker_prefixes(self) -> None:
        for prefix in ("a1b2c3d4", "A1B2C3D4"):
            with self.subTest(prefix=prefix):
                agents_log.append(prefix, "worker", None, AT)
                before = self.log_path.read_bytes()

                wrote = agents_log.register_default(prefix + "-1234-4567-89ab-123456789abc", "interactive", AT)

                self.assertIs(wrote, False)
                self.assertEqual(self.log_path.read_bytes(), before)

    def test_register_default_preserves_future_and_approximate_assignments(self) -> None:
        agents_log.append("future-agent", "director", None, AT + timedelta(days=1))
        agents_log.append("backfilled-agent", "worker", None, AT - timedelta(days=1), approximate=True)
        before = self.log_path.read_bytes()

        for agent_id in ("future-agent", "backfilled-agent"):
            with self.subTest(agent_id=agent_id):
                self.assertIs(agents_log.register_default(agent_id, "interactive", AT), False)

        self.assertEqual(self.log_path.read_bytes(), before)

    def test_register_default_rejects_unrelated_nonhex_and_wrong_length_prefix_matches(self) -> None:
        for recorded_id, full_id in (
            ("deadbeef", "a1b2c3d4-1234-4567-89ab-123456789abc"),
            ("1122334", "11223344-1234-4567-89ab-123456789abc"),
            ("556677889", "556677889-1234-4567-89ab-123456789abc"),
            ("zzzzzzzz", "zzzzzzzz-1234-4567-89ab-123456789abc"),
        ):
            with self.subTest(recorded_id=recorded_id):
                agents_log.append(recorded_id, "worker", None, AT)

                self.assertIs(agents_log.register_default(full_id, "interactive", AT), True)

                self.assertEqual(agents_log.assignments(self.log_path).assignments[full_id], (
                    agents_log.Assignment(AT, "interactive", None, False),
                ))

    def test_register_default_skips_malformed_matches_and_separates_a_truncated_tail(self) -> None:
        self.state.mkdir()
        malformed = [
            "not JSON",
            json.dumps({"id": "session-one", "role": "interactive", "at": "invalid"}),
            json.dumps({"id": "session-one", "role": "unknown", "at": AT.isoformat()}),
        ]
        _ = self.log_path.write_text("\n".join(malformed) + '\n{"id":"session-one"', encoding="utf-8")

        self.assertIs(agents_log.register_default("session-one", "interactive", AT), True)

        log = agents_log.assignments(self.log_path)
        self.assertEqual(len(log.problems), 4)
        self.assertEqual(log.assignments, {"session-one": (
            agents_log.Assignment(AT, "interactive", None, False),
        )})
        before = self.log_path.read_bytes()
        self.assertIs(agents_log.register_default("session-one", "interactive", AT), False)
        self.assertEqual(self.log_path.read_bytes(), before)

    def test_register_cli_accepts_new_roles_and_does_not_reassign_existing_ids(self) -> None:
        for role in ("interactive", "subagent"):
            with self.subTest(role=role):
                result = self.cli("register", "--id", role + "-id", "--role", role)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
        before = self.log_path.read_bytes()

        repeated = self.cli("register", "--id", "interactive-id", "--role", "subagent")

        self.assertEqual((repeated.returncode, repeated.stderr), (0, ""))
        self.assertEqual(self.log_path.read_bytes(), before)
        records = self.records()
        self.assertEqual([(row["id"], row["role"]) for row in records],
                         [("interactive-id", "interactive"), ("subagent-id", "subagent")])
        for row in records:
            self.assertEqual(set(row), {"id", "role", "at"})
            at = datetime.fromisoformat(cast(str, row["at"]))
            self.assertEqual(at.microsecond, 0)
            self.assertEqual(at.utcoffset(), timedelta(0))

    def test_register_cli_rejects_unknown_roles_and_missing_arguments_without_writing(self) -> None:
        for arguments in (("--id", "session-one", "--role", "unknown"),
                          ("--role", "interactive"), ("--id", "session-one")):
            with self.subTest(arguments=arguments):
                result = self.cli("register", *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage:", result.stderr)
                self.assertFalse(self.log_path.exists())

    def test_register_cli_write_failure_reports_one_line_and_keeps_exit_zero(self) -> None:
        self.log_path.mkdir(parents=True)

        result = self.cli("register", "--id", "session-one", "--role", "interactive")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith("agents_log: "), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_concurrent_default_registration_writes_one_assignment_and_returns_one_true(self) -> None:
        script = """
import json
import sys
from datetime import datetime, timezone
from scripts.usage.agents_log import register_default
print('ready', flush=True)
sys.stdin.readline()
print(json.dumps(register_default('shared-session', 'interactive', datetime.now(timezone.utc))))
"""
        processes: list[subprocess.Popen[str]] = []
        outcomes: list[bool] = []
        try:
            for _worker in range(6):
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", script],
                    cwd=REPOSITORY, env=os.environ.copy(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
            for process in processes:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline(), "ready\n")
            for process in processes:
                assert process.stdin is not None
                _ = process.stdin.write("go\n")
                process.stdin.flush()
            for process in processes:
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual((process.returncode, errors), (0, ""))
                wrote = cast(object, json.loads(output))
                self.assertIsInstance(wrote, bool)
                assert isinstance(wrote, bool)
                outcomes.append(wrote)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                _ = process.communicate()

        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(outcomes.count(False), 5)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["id"], records[0]["role"]), ("shared-session", "interactive"))
        self.assertEqual(agents_log.assignments(self.log_path).problems, ())

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

    def test_invalid_utf8_lines_are_counted_and_surrounding_assignments_load(self) -> None:
        agents_log.append("earlier-thread", "impl", None, AT)
        with self.log_path.open("ab") as handle:
            _ = handle.write(b'\xff\n{"id":"bad-\xff","role":"worker","at":"2026-10-11T17:30:00+00:00"}\n')
        agents_log.append("later-thread", "review", "contract", AT + timedelta(seconds=1))

        log = agents_log.assignments(self.log_path)

        self.assertEqual(set(log.assignments), {"earlier-thread", "later-thread"})
        self.assertEqual(len(log.problems), 2)
        for number, problem in enumerate(log.problems, 2):
            self.assertIn(f"{self.log_path}:{number}:", problem)
            self.assertIn("utf-8", problem)

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

    def test_role_at_merges_full_and_prefix_histories_by_time_and_keeps_prefix_lookup(self) -> None:
        prefix = "a1b2c3d4"
        session_id = prefix + "-1234-4567-89ab-123456789abc"
        initial = agents_log.Assignment(AT, "interactive", None)
        worker = agents_log.Assignment(AT + timedelta(seconds=1), "worker", None)
        director = agents_log.Assignment(AT + timedelta(seconds=2), "director", None)
        agents_log.append(session_id, director.role, director.lens, director.at)
        agents_log.append(prefix, worker.role, worker.lens, worker.at)
        agents_log.append(session_id, initial.role, initial.lens, initial.at)

        log = agents_log.assignments(self.log_path)

        self.assertEqual(agents_log.role_at(log, session_id, AT - timedelta(seconds=1)),
                         agents_log.Unassigned())
        for expected in (initial, worker, director):
            with self.subTest(role=expected.role):
                self.assertEqual(agents_log.role_at(log, session_id, expected.at), expected)
        self.assertEqual(agents_log.role_at(log, prefix, AT), agents_log.Unassigned())
        self.assertEqual(agents_log.role_at(log, prefix, director.at), worker)

    def test_role_at_ignores_unrelated_nonhex_and_wrong_length_prefixes(self) -> None:
        for recorded_id, session_id in (
            ("deadbeef", "a1b2c3d4-1234-4567-89ab-123456789abc"),
            ("1122334", "11223344-1234-4567-89ab-123456789abc"),
            ("556677889", "556677889-1234-4567-89ab-123456789abc"),
            ("zzzzzzzz", "zzzzzzzz-1234-4567-89ab-123456789abc"),
        ):
            with self.subTest(recorded_id=recorded_id):
                agents_log.append(session_id, "interactive", None, AT)
                agents_log.append(recorded_id, "worker", None, AT + timedelta(seconds=1))
                log = agents_log.assignments(self.log_path)

                self.assertEqual(agents_log.role_at(log, session_id, AT + timedelta(seconds=2)),
                                 agents_log.Assignment(AT, "interactive", None))

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
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
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

    def test_backfill_reads_runs_and_directors_under_both_run_folder_names(self) -> None:
        archive, live, _roster_at, _ledger_at = self.backfill_fixture()
        former = self.root / "former"
        former_run = former / "former-run"
        former_run.mkdir(parents=True)
        _ = (former_run / "mesh_roster.json").write_text(json.dumps({
            "project-fix": {"thread_id": "former-thread", "role": "fix", "status": "done"},
        }), encoding="utf-8")
        (former / "active").mkdir()
        _ = (former / "active" / "former-director").write_text(f"{former_run}\n", encoding="utf-8")

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live), "--live", str(former))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.assignments["former-thread"][0].role, "fix")
        self.assertEqual(log.assignments["former-director"][0].role, "director")
        self.assertEqual(log.assignments["director-session"][0].role, "director")

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

    def test_remote_role_reader_loads_without_a_worker_checkout(self) -> None:
        program = """import sys, types
for name in ('scripts', 'scripts.usage'):
    package = types.ModuleType(name)
    package.__path__ = []
    sys.modules[name] = package
module = types.ModuleType('scripts.usage.agents_log')
module.__file__ = '/scripts/usage/agents_log.py'
module.__package__ = 'scripts.usage'
sys.modules[module.__name__] = module
exec(compile(sys.stdin.read(), module.__file__, 'exec'), module.__dict__)
"""
        result = subprocess.run([sys.executable, "-I", "-c", program],
                                input=Path(agents_log.__file__).read_text(),
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_archive_backfill_links_the_old_ledger_and_preserves_its_timestamp(self) -> None:
        archive, live, _, ledger_at = self.backfill_fixture()
        run = archive / "archived-run"
        current = run / "workers"
        previous = run / "seats"
        _ = current.rename(previous)
        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertTrue(previous.samefile(current))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.assignments["claude-one"],
                         (agents_log.Assignment(ledger_at, "worker", None, True),))
        before = self.log_path.read_bytes()
        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(self.log_path.read_bytes(), before)

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

    def test_backfill_recovers_swapped_review_lenses_from_logs_naming_each_worker(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        run = archive / "archived-run"
        _ = (run / "mesh_roster.json").write_text(json.dumps({
            "project-test": {"thread_id": "contract-thread", "role": "review"},
            "project-impl": {"thread_id": "adversary-thread", "role": "review"},
        }), encoding="utf-8")
        _ = (run / "review_status_contract").write_text("reviewed\n", encoding="utf-8")
        _ = (run / "review_agent_adversary.log").write_text(
            "Review worker project-impl starts\n", encoding="utf-8")
        _ = (run / "review_agent_1_contract.log").write_text(
            "Review worker project-test starts\n", encoding="utf-8")

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        log = agents_log.assignments(self.log_path)
        self.assertEqual(log.assignments["contract-thread"][0].lens, "contract")
        self.assertEqual(log.assignments["adversary-thread"][0].lens, "adversary")

    def test_backfill_uses_the_newest_review_log_that_names_the_full_worker(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        run = archive / "archived-run"
        old_log = run / "review_agent_adversary.log"
        newest_matching = run / "review_agent_2_contract.log"
        _ = newest_matching.write_text("worker=project-test; role=review\n", encoding="utf-8")
        unrelated = run / "review_agent_3_ux.log"
        _ = unrelated.write_text("workers: project-test2 other-project-test\n", encoding="utf-8")
        for index, path in enumerate((old_log, newest_matching, unrelated)):
            stamp = 1_700_000_000 + index * 100
            os.utime(path, (stamp, stamp))

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(agents_log.assignments(self.log_path).assignments["archived-thread"][-1].lens,
                         "contract")

    def test_backfill_reads_review_logs_in_phase_directories(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        run = archive / "archived-run"
        old_log = run / "review_agent_adversary.log"
        # A worker seat's log carries its slot after the lens.
        phase_log = run / "phases" / "4" / "review_agent_2_contract-review2.log"
        phase_log.parent.mkdir(parents=True)
        _ = phase_log.write_text("worker=project-test; role=review\n", encoding="utf-8")
        for index, path in enumerate((old_log, phase_log)):
            stamp = 1_700_000_000 + index * 100
            os.utime(path, (stamp, stamp))

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(agents_log.assignments(self.log_path).assignments["archived-thread"][-1].lens,
                         "contract")

    def test_backfill_leaves_review_lens_absent_without_a_log_naming_the_worker(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        run = archive / "archived-run"
        _ = (run / "review_agent_adversary.log").write_text(
            "Review worker another-test starts\n", encoding="utf-8")

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

    def test_backfill_recovers_roster_when_the_same_runs_workers_ledger_is_unreadable(self) -> None:
        archive, live, _, _ = self.backfill_fixture()
        workers = archive / "archived-run/workers"
        _ = workers.write_bytes(b"\xff")

        result = self.cli("backfill", "--archive", str(archive), "--live", str(live))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn(str(workers), result.stderr)
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
