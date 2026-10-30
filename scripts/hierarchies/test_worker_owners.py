"""Worker ownership checks using temporary rosters and history records."""
from __future__ import annotations

from contextlib import AbstractContextManager, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from collections.abc import Iterator
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from typing import cast, override

from scripts.hierarchies import worker_owners as owners
from scripts.message import roster


class WorkerOwnersTest(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for folder in ("unit", "pool", "shutdown", "notifiers"):
            (self.root / folder).mkdir()
        _ = self.enterContext(cast(AbstractContextManager[object], patch.dict(os.environ, {
            "ROSTER_RUN_ROOT": str(self.root / "unit"),
            "REVIEW_POOL_STATE": str(self.root / "pool"),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "WORKER_MESSAGE_LOG": str(self.root / "messages.jsonl"),
            "WORKER_OWNERS_STATE": str(self.root / "state"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifiers"),
        })))
        self.write_text("messages.jsonl", "")

    def write_json(self, relative: str, record: object) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(record), encoding="utf-8")

    def write_text(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")

    def worker(
        self, *, name: str = "test-worker", thread: str = "thread-one",
        run: str = "unit/run-one", unit: str = "hierarchies", director: str = "director-one",
        classification: str = "OwnerUnknown", evidence: tuple[str, ...] = (),
        status: owners.WorkerTurnState = owners.WorkerTurnState.Idle,
    ) -> owners.Worker:
        ownership: owners.WorkerOwnership = owners.MissingDirectorRecord(unit)
        if classification == "OwnedLive":
            ownership = owners.LiveDirectorOwner(owners.Director(director, "", unit))
        elif director:
            ownership = owners.RecordedDirectorOwner(owners.Director(director, "", unit))
        return owners.Worker(name, thread, str(self.root / run), ownership, evidence, status=status)

    def message_record(self, address: str, sender: str = "unit-director") -> dict[str, object]:
        return {
            "time": "2026-10-28T20:00:00+00:00", "machine": "natedev", "from": sender,
            "to": address, "key": "fixture-key", "summary": "Assignment",
            "text": "Please inspect the assigned file.", "outcome": "sent", "detail": "queued to worker",
            "via": "queue", "postmaster": "delivered", "seconds": 0.01,
        }

    def director_entry(self, session: str = "director-one", account: str = "account-one") -> roster.RosterEntry:
        return roster.RosterEntry(
            machine="natedev", kind="claude", role="unit director", name="unit-director", status="running",
            reach=roster.Messageable("session:" + session), cwd="/work/hierarchies",
            showrunner=roster.NoShowrunner(), director=roster.DirectsUnit("hierarchies"),
            assignment=roster.NotAWorker(), session=roster.ClaudeSession(session), account=roster.AccountLabel(account),
        )

    def unknown_entry(self, thread: str = "thread-lost", name: str = "lost-worker") -> roster.RosterEntry:
        return roster.RosterEntry(
            machine="natedev", kind="codex", role="worker", name=name, status="paused",
            reach=roster.CannotReceiveMessages("run folder missing"), cwd="", showrunner=roster.NoShowrunner(),
            director=roster.NoDirector(), assignment=roster.NotAWorker(), thread=roster.CodexThread(thread),
        )

    def collect(self, entries: list[roster.RosterEntry], problems: tuple[str, ...] = ()) -> owners.OwnershipSources:
        observed = roster.Roster(entries, list(problems))
        with patch.object(roster, "roster", return_value=observed):
            return owners.collect_sources()

    def mesh_route(self, worker: owners.Worker, thread: str = "") -> None:
        self.write_json(str(Path(worker.run) / "mesh_roster.json"), {
            worker.name: {"thread_id": thread or worker.thread_id, "status": "paused"},
        })

    def pending_records(self, state: Path) -> list[owners.JsonObject]:
        path = state / "end-cleanup.json"
        if not path.exists():
            return []
        problems: list[str] = []
        fields = owners.read_object(path, problems)
        self.assertEqual(problems, [])
        return [owners.object_fields(item) for item in owners.list_items(fields.get("pending"))]

    def seed_pending_cleanup(self, worker: owners.Worker) -> Path:
        state = self.root / "state"
        self.write_json(str(Path(worker.run) / "mesh_roster.json"), {
            worker.name: {"thread_id": worker.thread_id, "status": "ended"},
        })
        self.write_json(str(state / "end-cleanup.json"), {"pending": [{
            "name": worker.name, "thread_id": worker.thread_id, "run": worker.run,
            "classification": worker.classification.value, "started_at": "2026-10-29T20:00:00+00:00",
        }]})
        return state

    def test_failed_end_saves_cleanup_before_handler_and_rerun_finishes_same_thread(self) -> None:
        worker = self.worker(classification="DirectorGone")
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        state = self.root / "state"
        commands: list[list[str]] = []

        def handler(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            self.assertEqual(options.get("timeout"), 45)
            self.assertTrue(options.get("capture_output"))
            self.assertEqual(command[command.index("--expected-thread-id") + 1], worker.thread_id)
            self.assertEqual(command[command.index("--session-dir") + 1], worker.run)
            self.assertEqual(command[command.index("--to") + 1], worker.name)
            pending = self.pending_records(state)
            self.assertEqual(len(pending), 1)
            self.assertEqual({key: pending[0][key] for key in ("name", "thread_id", "run", "classification")},
                             {"name": worker.name, "thread_id": worker.thread_id, "run": worker.run,
                              "classification": "DirectorGone"})
            started = datetime.fromisoformat(owners.text_field(pending[0], "started_at"))
            self.assertEqual(started.utcoffset(), timezone.utc.utcoffset(started))
            if "--cleanup-only" in command:
                self.assertNotIn("--idle-only", command)
                return subprocess.CompletedProcess(command, 0, "", "")
            self.assertIn("--idle-only", command)
            self.write_json(str(Path(worker.run) / "mesh_roster.json"), {
                worker.name: {"thread_id": worker.thread_id, "status": "ended"},
            })
            return subprocess.CompletedProcess(command, 1, "", "cleanup failed")

        with patch.object(owners, "collect_sources", return_value=sources), patch.object(subprocess, "run", side_effect=handler):
            first = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
            failed = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
        self.assertTrue(first.first_run)
        self.assertEqual(failed.failed, (worker.thread_id,))
        self.assertEqual(failed.ended, ())
        self.assertEqual(len(self.pending_records(state)), 1)
        empty = owners.OwnershipSources([], [], [], ["unrelated source cannot be read"])
        with patch.object(subprocess, "run", side_effect=handler):
            finished = owners.end_workers(empty, owners.WorkerClass.OwnerUnknown, state, apply=True)
        self.assertEqual(len(commands), 2)
        self.assertIn("--cleanup-only", commands[1])
        self.assertEqual(finished.failed, ())
        self.assertIn(worker.name + ": end cleanup finished", finished.evidence)
        self.assertEqual(self.pending_records(state), [])
        self.assertIn("end cleanup finished", (state / "ends.jsonl").read_text())

    def test_unreadable_pending_roster_keeps_cleanup_until_valid_ended_route_returns(self) -> None:
        worker = self.worker(classification="DirectorGone")
        state = self.seed_pending_cleanup(worker)
        pending = self.pending_records(state)
        route = Path(worker.run) / "mesh_roster.json"
        self.write_text(str(route), "{invalid JSON")
        sources = owners.OwnershipSources([], [], [], [])

        with patch.object(subprocess, "run") as handler:
            kept = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
        handler.assert_not_called()
        self.assertEqual(kept.failed, (worker.thread_id,))
        self.assertEqual(kept.ended, ())
        self.assertEqual(self.pending_records(state), pending)
        note = next(note for note in kept.evidence if "roster unreadable:" in note)
        self.assertTrue(note.startswith(worker.name + ": end cleanup kept: roster unreadable: "))
        self.assertIn(str(route), note)
        self.assertIn(note.removeprefix(worker.name + ": "), (state / "ends.jsonl").read_text())

        self.write_json(str(route), {worker.name: {"thread_id": worker.thread_id, "status": "ended"}})
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as handler:
            finished = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
        self.assertEqual(handler.call_count, 1)
        command = cast(list[str], handler.call_args.args[0])
        self.assertIn("--cleanup-only", command)
        self.assertEqual(command[command.index("--expected-thread-id") + 1], worker.thread_id)
        self.assertEqual(finished.failed, ())
        self.assertIn(worker.name + ": end cleanup finished", finished.evidence)
        self.assertEqual(self.pending_records(state), [])

    def test_failed_first_end_keeps_cleanup_when_roster_becomes_unreadable(self) -> None:
        worker = self.worker(classification="DirectorGone")
        self.mesh_route(worker)
        state = self.root / "state"
        sources = owners.OwnershipSources([worker], [], [], [])
        route = Path(worker.run) / "mesh_roster.json"

        def failed_end(selected: owners.Worker) -> int:
            self.assertEqual(selected.thread_id, worker.thread_id)
            self.assertEqual(len(self.pending_records(state)), 1)
            self.write_text(str(route), "{incomplete roster")
            return 1

        first = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
        self.assertTrue(first.first_run)
        with patch.object(owners, "collect_sources", return_value=sources), patch.object(subprocess, "run") as handler:
            failed = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state,
                                       apply=True, end_worker=failed_end)
        handler.assert_not_called()
        self.assertEqual(failed.failed, (worker.thread_id,))
        self.assertEqual(failed.ended, ())
        pending = self.pending_records(state)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["thread_id"], worker.thread_id)
        note = next(note for note in failed.evidence if "roster unreadable:" in note)
        self.assertTrue(note.startswith(worker.name + ": end cleanup kept: roster unreadable: "))
        self.assertIn(str(route), note)
        self.assertIn(note.removeprefix(worker.name + ": "), (state / "ends.jsonl").read_text())

    def test_finished_cleanup_is_logged_before_pending_journal_rewrite(self) -> None:
        worker = self.worker(classification="DirectorGone")
        state = self.seed_pending_cleanup(worker)
        pending = self.pending_records(state)
        sources = owners.OwnershipSources([], [], [], [])

        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as handler, \
                patch.object(owners, "write_pending_ends", side_effect=OSError("journal rewrite interrupted")):
            with self.assertRaisesRegex(OSError, "journal rewrite interrupted"):
                _ = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(self.pending_records(state), pending)
        outcomes = [owners.object_fields(cast(object, json.loads(line)))
                    for line in (state / "ends.jsonl").read_text().splitlines()]
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["outcome"], "end cleanup finished")
        self.assertEqual(owners.object_fields(outcomes[0]["worker"])["thread_id"], worker.thread_id)

        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as handler:
            finished = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(finished.failed, ())
        self.assertEqual(self.pending_records(state), [])
        self.assertEqual((state / "ends.jsonl").read_text().count('"outcome": "end cleanup finished"'), 2)

    def test_interrupted_end_leaves_cleanup_record_that_next_run_finishes(self) -> None:
        worker = self.worker(classification="DirectorGone")
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        state = self.root / "state"

        def interrupted(selected: owners.Worker) -> int:
            self.assertEqual(len(self.pending_records(state)), 1)
            self.write_json(str(Path(selected.run) / "mesh_roster.json"), {
                selected.name: {"thread_id": selected.thread_id, "status": "ended"},
            })
            raise KeyboardInterrupt

        first = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=interrupted)
        self.assertTrue(first.first_run)
        with self.assertRaises(KeyboardInterrupt):
            _ = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=interrupted)
        self.assertEqual(len(self.pending_records(state)), 1)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as handler:
            result = owners.end_workers(owners.OwnershipSources([], [], [], []), owners.WorkerClass.DirectorGone,
                                        state, apply=True)
        self.assertEqual(handler.call_count, 1)
        self.assertIn(worker.name + ": end cleanup finished", result.evidence)
        self.assertEqual(self.pending_records(state), [])

    def test_pending_cleanup_drops_reopened_replaced_or_missing_routes_without_handler(self) -> None:
        for change in ("reopened", "replaced", "missing-entry", "missing-run"):
            with self.subTest(change=change):
                worker = self.worker(name=change, run="unit/" + change, classification="DirectorGone")
                state = self.seed_pending_cleanup(worker)
                route = Path(worker.run) / "mesh_roster.json"
                if change == "missing-run":
                    route.unlink()
                    route.parent.rmdir()
                else:
                    entry = {"thread_id": worker.thread_id, "status": "ended"}
                    if change == "reopened":
                        entry["status"] = "running"
                    elif change == "replaced":
                        entry["thread_id"] = "new-thread"
                    self.write_json(str(route), {} if change == "missing-entry" else {worker.name: entry})
                before = route.read_bytes() if route.exists() else b""
                with patch.object(subprocess, "run") as handler:
                    result = owners.end_workers(owners.OwnershipSources([], [], [], []), owners.WorkerClass.DirectorGone,
                                                state, apply=True)
                handler.assert_not_called()
                self.assertEqual(result.failed, ())
                self.assertTrue(any(note.startswith(worker.name + ": end cleanup dropped: ") for note in result.evidence))
                self.assertEqual(self.pending_records(state), [])
                self.assertEqual(route.read_bytes() if route.exists() else b"", before)
                self.assertIn("end cleanup dropped:", (state / "ends.jsonl").read_text())

    def test_unsupported_cleanup_handler_keeps_record_and_cli_returns_failure(self) -> None:
        worker = self.worker(classification="DirectorGone")
        state = self.seed_pending_cleanup(worker)
        output = io.StringIO()
        with patch.object(owners, "collect_sources", return_value=owners.OwnershipSources([], [], [], [])), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", "unknown flag")) as handler, \
                redirect_stdout(output):
            result = owners.main(["end", "--class", "DirectorGone", "--apply"])
        self.assertEqual(result, 1)
        command = cast(list[str], handler.call_args.args[0])
        self.assertIn("--cleanup-only", command)
        self.assertEqual(command[command.index("--expected-thread-id") + 1], worker.thread_id)
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(len(self.pending_records(state)), 1)
        self.assertIn(worker.name + ": end cleanup failed: exit 2", output.getvalue())
        self.assertIn("end cleanup failed: exit 2", (state / "ends.jsonl").read_text())

    def test_pending_cleanup_dry_run_previews_without_writing(self) -> None:
        worker = self.worker(classification="DirectorGone")
        state = self.seed_pending_cleanup(worker)
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        output = io.StringIO()
        with patch.object(owners, "collect_sources", return_value=owners.OwnershipSources([], [], [], [])), \
                patch.object(subprocess, "run") as handler, redirect_stdout(output):
            result = owners.main(["end", "--class", "DirectorGone", "--dry-run"])
        self.assertEqual(result, 0)
        handler.assert_not_called()
        self.assertIn(worker.name + ": would finish end cleanup", output.getvalue())
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertEqual(len(self.pending_records(state)), 1)

    def test_unsuccessful_end_keeps_cleanup_only_after_same_thread_was_ended(self) -> None:
        for failure in ("exit", "kept", "os-error", "timeout"):
            for saved_end in (False, True):
                with self.subTest(failure=failure, saved_end=saved_end):
                    worker = self.worker(name=failure + str(saved_end), run="unit/" + failure + str(saved_end),
                                         classification="DirectorGone")
                    self.mesh_route(worker)
                    state = self.root / (failure + str(saved_end))
                    sources = owners.OwnershipSources([worker], [], [], [])

                    def unsuccessful(selected: owners.Worker) -> int | owners.WorkerKept:
                        self.assertEqual(len(self.pending_records(state)), 1)
                        if saved_end:
                            self.write_json(str(Path(selected.run) / "mesh_roster.json"), {
                                selected.name: {"thread_id": selected.thread_id, "status": "ended"},
                            })
                        if failure == "kept":
                            return owners.WorkerKept("handler refused after validation")
                        if failure == "os-error":
                            raise OSError("cleanup connection closed")
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired(["fixture-end"], 45)
                        return 1

                    first = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state,
                                                apply=True, end_worker=unsuccessful)
                    self.assertTrue(first.first_run)
                    result = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state,
                                                 apply=True, end_worker=unsuccessful)
                    self.assertEqual(result.ended, ())
                    self.assertEqual(len(self.pending_records(state)), int(saved_end))
                    if saved_end:
                        self.assertIn(worker.thread_id, result.failed)

    def test_cleanup_success_with_replaced_thread_keeps_failure_until_next_route_check(self) -> None:
        worker = self.worker(classification="DirectorGone")
        state = self.seed_pending_cleanup(worker)

        def replaced_during_cleanup(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(command[command.index("--expected-thread-id") + 1], worker.thread_id)
            self.assertEqual(options.get("timeout"), 45)
            self.write_json(str(Path(worker.run) / "mesh_roster.json"), {
                worker.name: {"thread_id": "replacement", "status": "running"},
            })
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(subprocess, "run", side_effect=replaced_during_cleanup):
            result = owners.end_workers(owners.OwnershipSources([], [], [], []), owners.WorkerClass.DirectorGone,
                                        state, apply=True)
        self.assertEqual(result.failed, (worker.thread_id,))
        self.assertTrue(any("end cleanup failed:" in note for note in result.evidence))
        self.assertEqual(len(self.pending_records(state)), 1)
        with patch.object(subprocess, "run") as handler:
            dropped = owners.end_workers(owners.OwnershipSources([], [], [], []), owners.WorkerClass.DirectorGone,
                                         state, apply=True)
        handler.assert_not_called()
        self.assertTrue(any("end cleanup dropped:" in note for note in dropped.evidence))
        self.assertEqual(self.pending_records(state), [])

    def test_report_rejects_bad_pending_entry_without_losing_valid_cleanup(self) -> None:
        worker = self.worker(classification="DirectorGone")
        state = self.seed_pending_cleanup(worker)
        valid = self.pending_records(state)
        self.write_json(str(state / "end-cleanup.json"), {"pending": [
            {**valid[0], "thread_id": 42}, valid[0],
        ]})
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        output = io.StringIO()
        with patch.object(owners, "collect_sources", return_value=owners.OwnershipSources([], [], [], [])), \
                patch.object(subprocess, "run") as handler, redirect_stdout(output):
            self.assertEqual(owners.main(["report", "--json"]), 0)
        handler.assert_not_called()
        report = owners.object_fields(cast(object, json.loads(output.getvalue())))
        self.assertEqual(report["end_cleanup_pending"], valid)
        problems = owners.list_items(report["problems"])
        self.assertEqual(len(problems), 1)
        self.assertIn(str(state / "end-cleanup.json"), str(problems[0]))
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_report_reads_pending_cleanup_and_never_creates_or_changes_state(self) -> None:
        worker = self.worker(classification="DirectorGone")
        empty = owners.OwnershipSources([], [], [], [])
        output = io.StringIO()
        with patch.object(owners, "collect_sources", return_value=empty), redirect_stdout(output):
            self.assertEqual(owners.main(["report"]), 0)
        self.assertTrue(output.getvalue().splitlines()[-1].endswith("end_cleanup_pending=0"))
        self.assertFalse((self.root / "state").exists())
        state = self.seed_pending_cleanup(worker)
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch.object(owners, "collect_sources", return_value=empty), patch.object(subprocess, "run") as handler:
            for arguments in (["report"], ["report", "--json"]):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(owners.main(arguments), 0)
                if "--json" in arguments:
                    report = owners.object_fields(cast(object, json.loads(output.getvalue())))
                    self.assertEqual(report["end_cleanup_pending"], self.pending_records(state))
                else:
                    self.assertTrue(output.getvalue().splitlines()[-1].endswith("end_cleanup_pending=1"))
        handler.assert_not_called()
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_raw_rosters_include_paused_and_finished_workers_but_skip_ended_routes(self) -> None:
        self.write_text("unit/run-one/director", "director-one\n")
        self.write_json("unit/run-one/mesh_roster.json", {
            "paused": {"thread_id": "thread-paused", "status": "paused"},
            "finished": {"thread_id": "thread-finished", "status": "finished"},
            "ended": {"thread_id": "thread-ended", "status": "ended"},
            "moved": {"thread_id": "thread-moved", "role_redirect": "new-role"},
        })
        self.write_json("pool/review/mesh/account-one/mesh_roster.json", {
            "reviewer": {"thread_id": "thread-pool", "status": "paused"},
        })
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        sources = self.collect([self.director_entry()])
        self.assertEqual(sources.problems, [])
        self.assertEqual({worker.thread_id: worker.classification for worker in sources.workers}, {
            "thread-paused": owners.WorkerClass.OwnedLive,
            "thread-finished": owners.WorkerClass.OwnedLive,
        })
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        paused = next(worker for worker in sources.workers if worker.name == "paused")
        self.assertEqual((paused.run, paused.unit, paused.director),
                         (str(self.root / "unit/run-one"), "hierarchies", "director-one"))
        self.assertIn("recorded status paused", paused.evidence)

    def test_unit_reviewer_ownership_does_not_scan_the_retired_archive(self) -> None:
        archive = self.root / "pool"
        self.write_text("unit/run-one/director", "director-one\n")
        self.write_json("unit/run-one/mesh_roster.json", {
            "project-review": {"thread_id": "unit-review-thread", "status": "paused", "role": "review"},
        })
        original_glob = Path.glob

        def refuse_archive(path: Path, pattern: str, *, case_sensitive: bool | None = None,
                           recurse_symlinks: bool = False) -> Iterator[Path]:
            if path.is_relative_to(archive):
                raise AssertionError(f"ownership scanned the retired archive: {path}")
            return original_glob(path, pattern, case_sensitive=case_sensitive, recurse_symlinks=recurse_symlinks)

        with patch.object(Path, "glob", refuse_archive):
            sources = self.collect([self.director_entry()])
        self.assertEqual(sources.problems, [])
        self.assertEqual([(worker.name, worker.thread_id, worker.classification) for worker in sources.workers],
                         [("project-review", "unit-review-thread", owners.WorkerClass.OwnedLive)])

    def test_live_director_worktree_prefix_protects_named_worker_with_lost_run_metadata(self) -> None:
        sources = self.collect([
            self.director_entry(),
            self.unknown_entry("thread-matching", "hierarchies-test2"),
            self.unknown_entry("thread-different", "models-test2"),
            self.unknown_entry("thread-similar", "hierarchies2-test2"),
        ])
        workers = {worker.thread_id: worker for worker in sources.workers}
        self.assertEqual(sources.problems, [])
        self.assertEqual(sources.directors[0].worker_prefix, "hierarchies")
        self.assertEqual((workers["thread-matching"].classification, workers["thread-matching"].director,
                          workers["thread-matching"].unit),
                         (owners.WorkerClass.OwnedLive, "director-one", "hierarchies"))
        self.assertEqual(workers["thread-different"].classification, owners.WorkerClass.OwnerUnknown)
        self.assertEqual(workers["thread-similar"].classification, owners.WorkerClass.OwnerUnknown)

    def test_non_unit_session_prefix_does_not_assign_unknown_worker(self) -> None:
        result = owners.classify_worker(
            self.worker(name="hierarchies-test2", unit="", director=""),
            [owners.Director("non-director", "other-session", "", "hierarchies")],
        )
        self.assertEqual(result.classification, owners.WorkerClass.OwnerUnknown)

    def test_lost_run_shutdown_record_restores_workers_after_director_account_restart(self) -> None:
        deleted = str(self.root / "unit/deleted-run")
        self.write_json("shutdown/old-account.json", {
            "session": {"session_id": "director-old", "name": "unit-director", "host": {"unit": "hierarchies"}},
            "codex_seats": [{"seat": "lost-worker", "thread_id": "thread-lost", "run_dir": deleted}],
        })
        sources = self.collect([self.director_entry("director-new", "account-new"), self.unknown_entry()])
        self.assertEqual(sources.workers[0].classification, owners.WorkerClass.OwnerUnknown)
        recovered = owners.recover_sources(sources)
        self.assertEqual(recovered.problems, [])
        worker = recovered.workers[0]
        self.assertEqual((worker.classification, worker.director, worker.unit, worker.run),
                         (owners.WorkerClass.OwnedLive, "director-new", "hierarchies", deleted))
        self.assertEqual(worker.name, "lost-worker")
        self.assertTrue(any("2026-10-28" in item and "missing" in item for item in worker.evidence))
        self.assertFalse(Path(deleted).exists())

    def test_launcher_and_message_records_recover_only_the_named_threads(self) -> None:
        self.write_text("unit/run-one/director", "director-one\n")
        self.write_json("unit/run-one/impl_worker_test.json", {
            "family": "codex", "worker_id": "thread-launcher", "name": "test-worker",
        })
        self.write_json("unit/message-run/mesh_roster.json", {
            "message-worker": {"thread_id": "thread-message", "status": "paused"},
        })
        self.write_text("messages.jsonl", json.dumps(self.message_record("worker:message-run/message-worker")) + "\n")
        sources = self.collect([self.director_entry(), self.unknown_entry("thread-launcher"),
                                self.unknown_entry("thread-message"), self.unknown_entry("thread-unrecorded")])
        recovered = {worker.thread_id: worker for worker in owners.recover_sources(sources).workers}
        self.assertEqual(sources.problems, [])
        self.assertEqual({thread: worker.classification for thread, worker in recovered.items()}, {
            "thread-launcher": owners.WorkerClass.OwnedLive,
            "thread-message": owners.WorkerClass.OwnedLive,
            "thread-unrecorded": owners.WorkerClass.OwnerUnknown,
        })
        self.assertTrue(any("launcher record" in item for item in recovered["thread-launcher"].evidence))
        self.assertTrue(any("message log" in item for item in recovered["thread-message"].evidence))

    def test_old_run_director_record_uses_shutdown_unit_after_account_restart(self) -> None:
        self.write_text("unit/run-one/director", "director-old\n")
        self.write_json("unit/run-one/mesh_roster.json", {
            "test-worker": {"thread_id": "thread-one", "status": "paused"},
        })
        self.write_json("shutdown/old-account.json", {
            "session": {"session_id": "director-old", "name": "unit-director", "host": {"unit": "hierarchies"}},
            "workers": [{"name": "test-worker", "thread_id": "thread-one", "run_dir": str(self.root / "unit/run-one")}],
        })
        recovered = owners.recover_sources(self.collect([self.director_entry("director-new", "account-new")]))
        self.assertEqual(recovered.problems, [])
        self.assertEqual((recovered.workers[0].classification, recovered.workers[0].director),
                         (owners.WorkerClass.OwnedLive, "director-new"))

    def test_unreadable_roster_is_reported_without_losing_valid_workers(self) -> None:
        self.write_text("unit/bad/mesh_roster.json", "{invalid")
        self.write_json("unit/good/mesh_roster.json", {"one": {"thread_id": "thread-one", "status": "paused"}})
        sources = self.collect([])
        self.assertEqual([worker.thread_id for worker in sources.workers], ["thread-one"])
        self.assertTrue(any("unit/bad/mesh_roster.json" in problem for problem in sources.problems))

    def test_json_report_lists_ownerless_live_worker_and_does_not_create_state(self) -> None:
        observed = roster.Roster([self.unknown_entry()], [])
        output = io.StringIO()
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch.object(roster, "roster", return_value=observed), redirect_stdout(output):
            result = owners.main(["report", "--json"])
        self.assertEqual(result, 0)
        report = cast(dict[str, object], json.loads(output.getvalue()))
        workers = cast(list[dict[str, object]], report["workers"])
        self.assertEqual([(worker["thread_id"], worker["classification"]) for worker in workers],
                         [("thread-lost", "OwnerUnknown")])
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
        self.assertFalse((self.root / "state").exists())

    def test_empty_roster_keeps_other_workers_and_ends_reports_with_totals(self) -> None:
        self.write_text("unit/bad/mesh_roster.json", "")
        self.write_json("unit/good/mesh_roster.json", {
            "valid-worker": {"thread_id": "thread-valid", "status": "paused"},
        })
        for command in ("report", "recover"):
            with self.subTest(command=command):
                output = io.StringIO()
                with patch.object(roster, "roster", return_value=roster.Roster([], [])), redirect_stdout(output):
                    result = owners.main([command])
                self.assertEqual(result, 0)
                lines = output.getvalue().splitlines()
                self.assertTrue(lines[0].startswith("valid-worker\tOwnerUnknown"), output.getvalue())
                self.assertIn("thread=thread-valid", lines[0])
                problems = [line for line in lines if line.startswith("source problem:")]
                self.assertEqual(len(problems), 1)
                self.assertIn("unit/bad/mesh_roster.json", problems[0])
                self.assertIn("Expecting value", problems[0])
                self.assertLess(lines.index(problems[0]), len(lines) - 2)
                self.assertTrue(lines[-2].startswith("Timers:"))
                self.assertTrue(lines[-1].startswith("Totals:"))
                self.assertIn("OwnerUnknown=1", lines[-1])
                self.assertIn("source_problems=1", lines[-1])

    def test_timer_details_require_full_and_json_keeps_each_registration(self) -> None:
        self.write_text("notifiers/asked/conf", "TARGET=unit-director\nREQUESTED_BY=user\n")
        self.write_text("notifiers/approved/conf", "TARGET=unit-director\nAPPROVED_BY=user\n")
        self.write_text("notifiers/unasked/conf", "TARGET=unit-director\n")
        outputs: dict[str, str] = {}
        for flag in ("", "--full", "--json"):
            output = io.StringIO()
            with patch.object(roster, "roster", return_value=roster.Roster([], [])), redirect_stdout(output):
                result = owners.main(["report"] + ([flag] if flag else []))
            self.assertEqual(result, 0)
            outputs[flag] = output.getvalue()
        self.assertEqual(outputs[""].splitlines()[:-1], [
            "Timers: 3 registered, 1 with no recorded request (the showrunner checks these; --full lists each)",
        ])
        full_lines = outputs["--full"].splitlines()
        self.assertEqual({line.split("\t")[0] for line in full_lines[:-1]},
                         {"timer asked", "timer approved", "timer unasked"})
        self.assertIn("requested_by=user", outputs["--full"])
        self.assertIn("request provenance not recorded", outputs["--full"])
        self.assertEqual(full_lines[-1], outputs[""].splitlines()[-1])
        report = cast(dict[str, object], json.loads(outputs["--json"]))
        self.assertEqual(set(report), {"workers", "totals", "problems", "timers", "notes", "end_cleanup_pending"})
        self.assertEqual(report["end_cleanup_pending"], [])
        timers = cast(list[dict[str, object]], report["timers"])
        self.assertEqual({timer["name"]: timer["requested_by"] for timer in timers},
                         {"asked": "user", "approved": "user", "unasked": ""})
        self.assertEqual(set(cast(dict[str, object], report["totals"])), {kind.value for kind in owners.WorkerClass})

    def test_applied_end_keeps_worker_when_roster_or_timer_source_cannot_be_read(self) -> None:
        self.write_json("unit/good/mesh_roster.json", {
            "valid-worker": {"thread_id": "thread-valid", "status": "paused"},
        })
        self.write_json("state/first-report.json", {})
        for relative, content in (("unit/bad/mesh_roster.json", b""),
                                  ("notifiers/bad/conf", b"\xff")):
            with self.subTest(source=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_bytes(content)
                output = io.StringIO()
                with patch.object(roster, "roster", return_value=roster.Roster([], [])), \
                        patch.object(subprocess, "run") as end_call, redirect_stdout(output):
                    result = owners.main(["end", "--class", "OwnerUnknown", "--apply"])
                self.assertEqual(result, 1)
                end_call.assert_not_called()
                self.assertIn("valid-worker: kept: source problems", output.getvalue())
                self.assertIn("source_problems=1", output.getvalue().splitlines()[-1])
                path.unlink()

    def test_worker_with_a_live_director_is_owned(self) -> None:
        worker = self.worker()
        result = owners.classify_worker(worker, [owners.Director("director-one", "unit-director", "hierarchies")])
        self.assertEqual(result.classification, owners.WorkerClass("OwnedLive"))
        self.assertEqual((result.name, result.thread_id, result.run), (worker.name, worker.thread_id, worker.run))

    def test_worker_with_an_absent_recorded_director_is_director_gone(self) -> None:
        result = owners.classify_worker(self.worker(), [owners.Director("other-director", "other-unit", "other")])
        self.assertEqual(result.classification, owners.WorkerClass("DirectorGone"))
        self.assertEqual(result.director, "director-one")

    def test_worker_without_an_owner_remains_unknown(self) -> None:
        result = owners.classify_worker(self.worker(unit="", director=""), [])
        self.assertEqual(result.classification, owners.WorkerClass("OwnerUnknown"))


    def test_a_different_units_live_director_does_not_protect_worker(self) -> None:
        result = owners.classify_worker(
            self.worker(director="director-old"),
            [owners.Director("director-new", "other-director", "models")],
        )
        self.assertEqual(result.classification, owners.WorkerClass("DirectorGone"))

    def test_director_restart_on_another_account_preserves_unit_workers(self) -> None:
        result = owners.classify_worker(
            self.worker(director="director-old"),
            [owners.Director("director-new", "unit-director", "hierarchies")],
        )
        self.assertEqual(result.classification, owners.WorkerClass("OwnedLive"))

    def test_missing_run_folder_recovers_owner_by_thread_id(self) -> None:
        worker = self.worker(run="unit/deleted-run", unit="", director="")
        prior = owners.OwnershipEvidence.from_worker(
            self.worker(run="unit/deleted-run", evidence=("shutdown history: run folder removed 2026-10-28",)),
        )
        result = owners.recover_worker(
            worker, [prior], [owners.Director("director-one", "unit-director", "hierarchies")],
        )
        self.assertEqual(result.classification, owners.WorkerClass("OwnedLive"))
        self.assertEqual((result.unit, result.director), ("hierarchies", "director-one"))
        self.assertTrue(result.recovery_attempted)
        self.assertTrue(any("2026-10-28" in item for item in result.evidence))
        self.assertFalse(Path(worker.run).exists())

    def test_recovery_does_not_assign_a_same_named_workers_other_thread(self) -> None:
        worker = self.worker(unit="", director="")
        prior = owners.OwnershipEvidence.from_worker(self.worker(thread="different-thread", evidence=("launcher record",)))
        result = owners.recover_worker(
            worker, [prior], [owners.Director("director-one", "unit-director", "hierarchies")],
        )
        self.assertEqual(result.classification, owners.WorkerClass("OwnerUnknown"))
        self.assertEqual((result.unit, result.director), ("", ""))
        self.assertTrue(result.recovery_attempted)

    def test_recovery_keeps_a_gone_director_distinct_from_an_unknown_owner(self) -> None:
        worker = self.worker(unit="", director="")
        prior = owners.OwnershipEvidence.from_worker(self.worker(evidence=("message log naming thread-one",)))
        result = owners.recover_worker(worker, [prior], [])
        self.assertEqual(result.classification, owners.WorkerClass("DirectorGone"))
        self.assertEqual(result.director, "director-one")
        self.assertTrue(result.recovery_attempted)

    def test_recovery_retains_ownership_when_shutdown_and_launcher_records_agree(self) -> None:
        worker = self.worker(unit="", director="")
        records = [owners.OwnershipEvidence.from_worker(self.worker(evidence=(source,)))
                   for source in ("shutdown history", "launcher record")]
        result = owners.recover_worker(
            worker, records, [owners.Director("director-one", "unit-director", "hierarchies")],
        )
        self.assertEqual(result.classification, owners.WorkerClass("OwnedLive"))
        self.assertEqual(result.thread_id, "thread-one")
        self.assertTrue(any("shutdown history" in item for item in result.evidence))
        self.assertTrue(any("launcher record" in item for item in result.evidence))

    def test_first_applied_end_reports_then_only_ends_the_selected_gone_worker(self) -> None:
        gone = self.worker(name="gone", thread="gone-thread", director="director-gone")
        live = self.worker(name="live", thread="live-thread")
        unknown = self.worker(name="unknown", thread="unknown-thread", unit="", director="")
        sources = owners.OwnershipSources([gone, live, unknown],
                                         [owners.Director("director-one", "unit-director", "other-unit")], [], [])
        self.mesh_route(gone)
        calls: list[owners.Worker] = []

        def end(worker: owners.Worker) -> int:
            calls.append(worker)
            return 0

        state = self.root / "end-state"
        first = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.assertTrue(first.first_run)
        self.assertEqual((first.ended, calls), ((), []))
        self.assertTrue((state / "first-report.json").exists())
        dry = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, end_worker=end)
        self.assertFalse(dry.first_run)
        self.assertEqual((dry.ended, calls), ((), []))
        applied = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.assertEqual(applied.ended, (gone.thread_id,))
        self.assertEqual([worker.thread_id for worker in calls], [gone.thread_id])
        log = (state / "ends.jsonl").read_text(encoding="utf-8")
        self.assertIn("director-gone", log)
        self.assertIn("end exit 0", log)

    def test_unknown_end_recovers_live_owner_and_only_ends_failed_recovery(self) -> None:
        recovered = self.worker(name="recovered", thread="recovered-thread", unit="", director="")
        unknown = self.worker(name="unknown", thread="unknown-thread", unit="", director="")
        prior = owners.OwnershipEvidence.from_worker(
            self.worker(name="recovered", thread="recovered-thread", evidence=("shutdown history",)),
        )
        sources = owners.OwnershipSources([recovered, unknown],
                                         [owners.Director("director-one", "unit-director", "hierarchies")], [prior], [])
        self.mesh_route(unknown)
        calls: list[owners.Worker] = []

        def end(worker: owners.Worker) -> int:
            calls.append(worker)
            return 0

        state = self.root / "end-state"
        _ = owners.end_workers(sources, owners.WorkerClass.OwnerUnknown, state, apply=True, end_worker=end)
        result = owners.end_workers(sources, owners.WorkerClass.OwnerUnknown, state, apply=True, end_worker=end)
        self.assertEqual(result.ended, ("unknown-thread",))
        self.assertEqual([worker.thread_id for worker in calls], ["unknown-thread"])
        self.assertTrue(calls[0].recovery_attempted)
        self.assertTrue(any("no owner found" in item for item in calls[0].evidence))
        self.assertEqual(result.workers[0].classification, owners.WorkerClass.OwnedLive)

    def test_conflicting_provenance_does_not_assign_one_gone_director(self) -> None:
        unknown = self.worker(director="", unit="")
        records = [owners.OwnershipEvidence.from_worker(self.worker(director=director, unit=unit))
                   for director, unit in (("old-one", "one"), ("old-two", "two"))]
        result = owners.recover_worker(unknown, records, [])
        self.assertEqual(result.classification, owners.WorkerClass.OwnerUnknown)
        self.assertTrue(result.recovery_attempted)
        self.assertTrue(any("conflicting" in item for item in result.evidence))

    def test_end_failure_is_logged_without_reporting_success(self) -> None:
        worker = self.worker()
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        calls: list[str] = []

        def end(selected: owners.Worker) -> int:
            calls.append(selected.thread_id)
            return 7

        state = self.root / "end-state"
        _ = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        result = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.assertEqual(calls, ["thread-one"])
        self.assertEqual(result.ended, ())
        self.assertTrue(any("end exit 7" in item for item in result.evidence))
        self.assertIn("end exit 7", (state / "ends.jsonl").read_text(encoding="utf-8"))

    def test_end_refuses_changed_thread_route_and_source_problems(self) -> None:
        worker = self.worker()
        self.mesh_route(worker, "replacement-thread")
        calls: list[owners.Worker] = []

        def end(selected: owners.Worker) -> int:
            calls.append(selected)
            return 0

        state = self.root / "end-state"
        sources = owners.OwnershipSources([worker], [], [], [])
        _ = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        changed = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.assertEqual((changed.ended, calls), ((), []))
        self.assertTrue(any("thread changed" in item for item in changed.evidence))
        self.mesh_route(worker)
        failed = owners.OwnershipSources([worker], [], [], ["director source unavailable"])
        blocked = owners.end_workers(failed, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.assertEqual((blocked.ended, calls), ((), []))
        self.assertTrue(any("source problems" in item for item in blocked.evidence))

    def test_end_never_accepts_live_classes(self) -> None:
        sources = owners.OwnershipSources([], [], [], [])

        def forbidden_end(worker: owners.Worker) -> int:
            self.fail("end callback reached for " + worker.name)

        for kind in (owners.WorkerClass.OwnedLive,):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                _ = owners.end_workers(sources, kind, self.root / "end-state", apply=True, end_worker=forbidden_end)

    def test_timers_distinguish_recorded_requests_from_missing_provenance(self) -> None:
        self.write_text("notifiers/asked/conf", "TARGET=unit-director\nREQUESTED_BY=user\n")
        self.write_text("notifiers/unasked/conf", "TARGET=unit-director\n")
        problems: list[str] = []
        timers = {item.name: item for item in owners.timer_registrations(self.root / "notifiers", problems)}
        self.assertEqual(problems, [])
        self.assertEqual(timers["asked"].requested_by, "user")
        self.assertEqual(timers["unasked"].requested_by, "")
        self.assertIn("not recorded", timers["unasked"].evidence)

    def test_dry_run_does_not_consume_first_apply(self) -> None:
        worker = self.worker()
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        calls: list[str] = []

        def end(selected: owners.Worker) -> int:
            calls.append(selected.thread_id)
            return 0

        state = self.root / "end-state"
        _ = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, end_worker=end)
        self.assertFalse((state / "first-report.json").exists())
        first = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.assertTrue(first.first_run)
        self.assertEqual((first.ended, calls), ((), []))

    def test_running_worker_is_never_passed_to_end(self) -> None:
        self.write_text("unit/run-one/director", "director-gone\n")
        self.write_json("unit/run-one/mesh_roster.json", {
            "active-worker": {"thread_id": "active-thread", "status": "running", "turn_id": "active-turn"},
        })
        sources = self.collect([])
        calls: list[str] = []

        def end(selected: owners.Worker) -> int:
            calls.append(selected.thread_id)
            return 0

        state = self.root / "end-state"
        _ = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        result = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.assertEqual((result.ended, calls), ((), []))

    def test_route_that_becomes_running_after_collection_is_kept(self) -> None:
        worker = self.worker()
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        calls: list[str] = []

        def end(selected: owners.Worker) -> int:
            calls.append(selected.thread_id)
            return 0

        state = self.root / "end-state"
        _ = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.write_json(str(Path(worker.run) / "mesh_roster.json"), {
            worker.name: {"thread_id": worker.thread_id, "status": "running", "turn_id": "new-turn"},
        })
        result = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.assertEqual((result.ended, calls), ((), []))

    def test_freestanding_thread_with_cwd_recovers_deleted_run_owner(self) -> None:
        deleted = str(self.root / "unit/deleted-run")
        self.write_json("shutdown/director.json", {
            "session": {"session_id": "director-old", "name": "unit-director", "host": {"unit": "hierarchies"}},
            "codex_seats": [{"thread_id": "thread-lost", "seat": "lost-worker", "run_dir": deleted}],
        })
        observed = replace(self.unknown_entry(), role="freestanding", cwd="/work/original-tree")
        recovered = owners.recover_sources(self.collect([self.director_entry("director-new"), observed]))
        self.assertEqual([worker.thread_id for worker in recovered.workers], ["thread-lost"])
        self.assertEqual((recovered.workers[0].classification, recovered.workers[0].director),
                         (owners.WorkerClass.OwnedLive, "director-new"))

    def test_freestanding_thread_without_worker_provenance_is_excluded(self) -> None:
        observed = replace(self.unknown_entry(), role="freestanding", cwd="/work/unrecorded-tree")
        sources = self.collect([observed])
        self.assertEqual(sources.workers, [])

    def test_mesh_end_refuses_a_route_replaced_during_final_source_read(self) -> None:
        worker = self.worker(classification="DirectorGone")
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        commands: list[list[str]] = []

        def collect_after_replacement() -> owners.OwnershipSources:
            self.mesh_route(worker, "live-replacement-thread")
            return sources

        def external(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            self.assertTrue(options)
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(owners, "collect_sources", side_effect=collect_after_replacement), \
                patch.object(subprocess, "run", side_effect=external):
            result = owners.mesh_end(worker)
        self.assertNotEqual(result, 0)
        self.assertEqual([command for command in commands if "end" in command], [])

    def test_mesh_end_requires_expected_thread_and_idle_guard_without_retry(self) -> None:
        worker = self.worker(classification="DirectorGone")
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        commands: list[list[str]] = []

        def unsupported_guard(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            self.assertTrue(options)
            commands.append(command)
            return subprocess.CompletedProcess(command, 2, "", "guard flags unsupported")

        with patch.object(owners, "collect_sources", return_value=sources), \
                patch.object(subprocess, "run", side_effect=unsupported_guard):
            result = owners.mesh_end(worker)
        self.assertEqual(result, 2)
        self.assertEqual(len(commands), 1)
        command = commands[0]
        expected = command.index("--expected-thread-id")
        self.assertEqual(command[expected + 1], worker.thread_id)
        self.assertIn("--idle-only", command)

    def test_replacement_at_handler_entry_keeps_status_marker_and_queue_unchanged(self) -> None:
        worker = self.worker(classification="DirectorGone")
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        run = Path(worker.run)
        queue = run / (worker.name + ".pending.json")
        marker = run / (worker.name + ".end")
        self.write_json(str(queue), {"thread_id": "replacement-thread", "messages": ["next task"]})
        commands: list[list[str]] = []
        replacement_files: list[dict[str, bytes]] = []

        def guarded_handler(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            self.assertTrue(options)
            commands.append(command)
            self.write_json(str(run / "mesh_roster.json"), {
                worker.name: {"thread_id": "replacement-thread", "status": "running", "turn_id": "replacement-turn"},
            })
            replacement_files.append({path.name: path.read_bytes() for path in run.iterdir() if path.is_file()})
            expected = command[command.index("--expected-thread-id") + 1] if "--expected-thread-id" in command else ""
            if expected and expected != "replacement-thread":
                return subprocess.CompletedProcess(command, 3, "", "expected thread mismatch")
            marker.touch()
            _ = queue.write_text("", encoding="utf-8")
            self.write_json(str(run / "mesh_roster.json"), {
                worker.name: {"thread_id": "replacement-thread", "status": "ended"},
            })
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(owners, "collect_sources", return_value=sources), \
                patch.object(subprocess, "run", side_effect=guarded_handler):
            result = owners.mesh_end(worker)
        self.assertNotEqual(result, 0)
        self.assertEqual(len(commands), 1)
        expected = commands[0].index("--expected-thread-id")
        self.assertEqual(commands[0][expected + 1], worker.thread_id)
        self.assertIn("--idle-only", commands[0])
        self.assertEqual({path.name: path.read_bytes() for path in run.iterdir() if path.is_file()}, replacement_files[0])

    def test_conflicting_mesh_records_keep_both_owners_and_never_report_gone(self) -> None:
        self.write_text("unit/a-live/director", "director-one\n")
        self.write_json("unit/a-live/mesh_roster.json", {
            "legacy-worker": {"thread_id": "shared-thread", "status": "paused"},
        })
        self.write_text("unit/z-stale/director", "director-gone\n")
        self.write_json("unit/z-stale/mesh_roster.json", {
            "legacy-worker": {"thread_id": "shared-thread", "status": "paused"},
        })
        sources = self.collect([self.director_entry()])
        recovered = owners.recover_sources(sources)
        self.assertEqual(len(recovered.workers), 1)
        worker = recovered.workers[0]
        self.assertEqual(worker.classification, owners.WorkerClass.OwnerUnknown)
        self.assertTrue(any("a-live" in item for item in worker.evidence))
        self.assertTrue(any("z-stale" in item for item in worker.evidence))
        calls: list[str] = []

        def end(selected: owners.Worker) -> int:
            calls.append(selected.thread_id)
            return 0

        for kind in (owners.WorkerClass.DirectorGone, owners.WorkerClass.OwnerUnknown):
            state = self.root / ("end-state-" + kind.value)
            _ = owners.end_workers(sources, kind, state, apply=True, end_worker=end)
            result = owners.end_workers(sources, kind, state, apply=True, end_worker=end)
            self.assertEqual((result.ended, calls), ((), []))

    def test_message_discussing_another_workers_thread_does_not_claim_ownership(self) -> None:
        thread = "11111111-2222-3333-4444-555555555555"
        self.write_text("messages.jsonl", json.dumps({
            "from": "unit-director",
            "text": "Other unit worker " + thread + " is paused in /tmp/claude/unit/other-run; please inspect it.",
        }) + "\n")
        recovered = owners.recover_sources(self.collect([self.director_entry(), self.unknown_entry(thread)]))
        self.assertEqual(recovered.workers[0].classification, owners.WorkerClass.OwnerUnknown)
        self.assertEqual(recovered.workers[0].director, "")

    def test_known_live_owner_does_not_run_historical_recovery(self) -> None:
        worker = self.worker(classification="OwnedLive", evidence=("current live roster",))
        history = owners.OwnershipEvidence.from_worker(
            self.worker(director="departed-director", unit="departed-unit", evidence=("old launcher record",)),
        )
        result = owners.recover_worker(worker, [history], [owners.Director("director-one", "unit-director", "hierarchies")])
        self.assertEqual(result.classification, owners.WorkerClass.OwnedLive)
        self.assertFalse(result.recovery_attempted)
        self.assertTrue(any("current live roster" in item for item in result.evidence))
        self.assertNotIn("old launcher record", result.evidence)

    def test_provenance_read_failure_blocks_applied_end_of_valid_roster_worker(self) -> None:
        self.write_text("unit/run-one/director", "departed-director\n")
        self.write_json("unit/run-one/mesh_roster.json", {
            "test-worker": {"thread_id": "thread-one", "status": "paused"},
        })
        self.write_text("messages.jsonl", "{invalid\n")
        sources = self.collect([])
        self.assertTrue(any("messages.jsonl" in problem for problem in sources.problems))
        calls: list[str] = []

        def end(worker: owners.Worker) -> int:
            calls.append(worker.thread_id)
            return 0

        state = self.root / "end-state"
        _ = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        result = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
        self.assertEqual((result.ended, calls), ((), []))
        self.assertTrue(any("source problems" in item for item in result.evidence))

    def test_unowned_history_is_evidence_not_a_current_worker_or_valid_director(self) -> None:
        records = owners.provenance_records({"thread_id": "historical-thread", "seat": "historical-worker"},
                                           "shutdown record")
        self.assertEqual(len(records), 1)
        self.assertNotIsInstance(records[0], owners.Worker)
        self.assertIsInstance(records[0].owner, owners.MissingDirectorRecord)
        recovered = owners.recover_worker(self.worker(thread="historical-thread", director="", unit=""), records, [])
        self.assertEqual(recovered.classification, owners.WorkerClass.OwnerUnknown)
        self.assertTrue(recovered.recovery_attempted)
        owned_records = owners.provenance_records({
            "thread_id": "recorded-thread", "director_session_id": "recorded-director", "unit": "hierarchies",
        }, "launcher record")
        self.assertEqual(owned_records[0].owner, owners.Director("recorded-director", "", "hierarchies"))
        with self.assertRaises(ValueError):
            _ = owners.Director("", "unrecorded-director", "")
        with self.assertRaises(ValueError):
            _ = self.worker(director="", classification="OwnedLive")

    def test_applied_end_failure_sets_nonzero_cli_exit(self) -> None:
        worker = self.worker(classification="DirectorGone")
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        original_end = owners.end_workers
        calls: list[str] = []

        def fail(selected: owners.Worker) -> int:
            calls.append(selected.thread_id)
            return 7

        def injected_end(
            current: owners.OwnershipSources, selected: owners.WorkerClass, state: Path, apply: bool = False,
        ) -> owners.EndResult:
            return original_end(current, selected, state, apply=apply, end_worker=fail)

        output = io.StringIO()
        with patch.object(owners, "collect_sources", return_value=sources), \
                patch.object(owners, "end_workers", side_effect=injected_end), redirect_stdout(output):
            first = owners.main(["end", "--class", "DirectorGone", "--apply", "--json"])
            failed = owners.main(["end", "--class", "DirectorGone", "--apply", "--json"])
        self.assertEqual(first, 0)
        self.assertEqual(calls, [worker.thread_id])
        self.assertNotEqual(failed, 0)

    def test_applied_end_timeout_sets_nonzero_cli_exit(self) -> None:
        worker = self.worker(classification="DirectorGone")
        self.mesh_route(worker)
        sources = owners.OwnershipSources([worker], [], [], [])
        original_end = owners.end_workers
        calls: list[str] = []

        def timeout(selected: owners.Worker) -> int:
            calls.append(selected.thread_id)
            raise subprocess.TimeoutExpired(["fixture-end", selected.thread_id], 45)

        def injected_end(
            current: owners.OwnershipSources, selected: owners.WorkerClass, state: Path, apply: bool = False,
        ) -> owners.EndResult:
            return original_end(current, selected, state, apply=apply, end_worker=timeout)

        output = io.StringIO()
        with patch.object(owners, "collect_sources", return_value=sources), \
                patch.object(owners, "end_workers", side_effect=injected_end), redirect_stdout(output):
            first = owners.main(["end", "--class", "DirectorGone", "--apply", "--json"])
            failed = owners.main(["end", "--class", "DirectorGone", "--apply", "--json"])
        self.assertEqual(first, 0)
        self.assertEqual(calls, [worker.thread_id])
        self.assertNotEqual(failed, 0)

    def test_capacity_waiters_are_kept_before_and_after_source_collection(self) -> None:
        for status in ("waiting_capacity", "waiting for capacity"):
            with self.subTest(status=status):
                self.write_text("unit/capacity/director", "departed-director\n")
                self.write_json("unit/capacity/mesh_roster.json", {
                    "queued-worker": {"thread_id": "queued-thread", "status": status, "launcher_pid": 42},
                })
                sources = self.collect([])
                calls: list[str] = []

                def end(worker: owners.Worker) -> int:
                    calls.append(worker.thread_id)
                    return 0

                state = self.root / ("state-" + status.replace(" ", "-"))
                first = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
                self.assertTrue(first.first_run)
                result = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
                self.assertEqual((result.ended, calls), ((), []))
                self.write_json("unit/capacity/mesh_roster.json", {
                    "queued-worker": {"thread_id": "queued-thread", "status": "paused"},
                })
                idle_sources = self.collect([])
                self.write_json("unit/capacity/mesh_roster.json", {
                    "queued-worker": {"thread_id": "queued-thread", "status": status, "launcher_pid": 42},
                })
                preview = owners.end_workers(idle_sources, owners.WorkerClass.DirectorGone, state)
                self.assertTrue(any("would keep" in item and "attached" in item for item in preview.evidence))
                changed = owners.end_workers(idle_sources, owners.WorkerClass.DirectorGone, state, apply=True, end_worker=end)
                self.assertEqual((changed.ended, calls), ((), []))
                with patch.object(owners, "collect_sources", return_value=idle_sources), patch.object(subprocess, "run") as external:
                    refusal = owners.mesh_end(idle_sources.workers[0])
                self.assertIsInstance(refusal, owners.WorkerKept)
                external.assert_not_called()

    def test_held_roles_and_unclaimed_daemon_threads_are_excluded_from_worker_totals(self) -> None:
        held = replace(self.unknown_entry("held-thread", "design-reviewer"), role="astra-reviewer",
                       cwd="/work/design", assignment=roster.WorkerAssignment("astra-reviewer", str(self.root / "unit/held")))
        self.write_json("unit/held/mesh_roster.json", {
            "design-reviewer": {"thread_id": "held-thread", "status": "paused", "held_design_id": "design-one"},
        })
        daemon = replace(self.unknown_entry("chat-thread", "interactive-chat"), role="freestanding", cwd="/work/chat")
        sources = self.collect([held, daemon])
        self.assertEqual(sources.workers, [])
        calls: list[str] = []

        def end(worker: owners.Worker) -> int:
            calls.append(worker.thread_id)
            return 0

        first = owners.end_workers(sources, owners.WorkerClass.OwnerUnknown, self.root / "held-end", apply=True, end_worker=end)
        self.assertTrue(first.first_run)
        result = owners.end_workers(sources, owners.WorkerClass.OwnerUnknown, self.root / "held-end", apply=True, end_worker=end)
        self.assertEqual((result.ended, calls), ((), []))

    def test_missing_or_changed_mesh_route_is_kept_with_successful_cli_exit(self) -> None:
        for replacement in ("", "replacement-thread"):
            with self.subTest(replacement=replacement):
                worker = self.worker()
                if replacement:
                    self.mesh_route(worker, replacement)
                sources = owners.OwnershipSources([worker], [], [], [])
                output = io.StringIO()
                with patch.object(owners, "collect_sources", return_value=sources), redirect_stdout(output):
                    first = owners.main(["end", "--class", "DirectorGone", "--apply", "--json"])
                    code = owners.main(["end", "--class", "DirectorGone", "--apply", "--json"])
                self.assertEqual(first, 0)
                self.assertEqual(code, 0)
                self.assertIn("kept: mesh route missing or thread changed", output.getvalue())

    def test_dry_run_previews_keep_reasons_and_only_eligible_end(self) -> None:
        self.write_text("unit/preview/director", "departed-director\n")
        self.write_json("unit/preview/mesh_roster.json", {
            "busy": {"thread_id": "busy-thread", "status": "running"},
            "ready": {"thread_id": "ready-thread", "status": "paused"},
        })
        sources = self.collect([])
        state = self.root / "preview-end"
        first = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
        self.assertTrue(first.first_run)
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        preview = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state)
        self.assertTrue(any("busy: would keep" in line and "attached" in line for line in preview.evidence))
        self.assertIn("ready: would end", preview.evidence)
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_report_does_not_recover_recorded_owners_or_repeat_evidence(self) -> None:
        self.write_text("unit/run-one/director", "director-one\n")
        self.write_json("unit/run-one/mesh_roster.json", {
            "test-worker": {"thread_id": "thread-one", "status": "paused"},
        })
        observed = replace(self.unknown_entry("thread-one", "test-worker"),
                           director=roster.UnderDirector("hierarchies", "unit-director", "session:director-one"),
                           assignment=roster.WorkerAssignment("impl", str(self.root / "unit/run-one")))
        sources = self.collect([self.director_entry(), observed])
        current = sources.workers[0]
        self.assertFalse(current.recovery_attempted)
        repeated = owners.recover_sources(owners.recover_sources(sources)).workers[0]
        self.assertFalse(repeated.recovery_attempted)
        self.assertEqual(repeated.evidence, current.evidence)
        self.assertEqual(len(repeated.evidence), len(set(repeated.evidence)))

    def test_recover_saves_only_workers_whose_missing_owners_were_found(self) -> None:
        self.write_text("unit/owned/director", "director-one\n")
        self.write_json("unit/owned/mesh_roster.json", {
            "owned": {"thread_id": "owned-thread", "status": "paused"},
        })
        self.write_text("unit/gone/director", "departed-director\n")
        self.write_json("unit/gone/mesh_roster.json", {
            "gone": {"thread_id": "gone-thread", "status": "paused"},
        })
        self.write_json("shutdown/lost-worker.json", {
            "session": {"session_id": "director-one", "name": "unit-director", "host": {"unit": "hierarchies"}},
            "codex_seats": [{"thread_id": "lost-thread", "seat": "lost", "run_dir": str(self.root / "unit/deleted")}],
        })
        observed = roster.Roster([self.director_entry(), self.unknown_entry("lost-thread", "lost")], [])
        with patch.object(roster, "roster", return_value=observed), redirect_stdout(io.StringIO()):
            code = owners.main(["recover", "--json"])
        self.assertEqual(code, 0)
        saved = cast(dict[str, object], json.loads((self.root / "state/recovered.json").read_text()))
        workers = cast(list[dict[str, object]], saved["workers"])
        self.assertEqual([item["thread_id"] for item in workers], ["lost-thread"])
        report = self.collect(observed.entries)
        owned = {item.thread_id: item for item in report.workers}
        self.assertEqual(owned["lost-thread"].classification, owners.WorkerClass.OwnedLive)
        self.assertFalse(owned["lost-thread"].recovery_attempted)
        self.assertFalse(owned["gone-thread"].recovery_attempted)

    def test_repeated_recover_keeps_saved_owners_after_original_evidence_is_removed(self) -> None:
        source = self.root / "unit/deleted/impl_worker_lost.json"
        self.write_json(str(source), {
            "thread_id": "lost-thread", "seat": "lost", "director_session_id": "director-one",
            "unit": "hierarchies", "run_dir": str(source.parent),
        })
        daemon = replace(self.unknown_entry("lost-thread", "lost"), role="freestanding", cwd="/work/lost")
        observed = roster.Roster([self.director_entry(), daemon], [])
        with patch.object(roster, "roster", return_value=observed), redirect_stdout(io.StringIO()):
            self.assertEqual(owners.main(["recover", "--json"]), 0)
            source.unlink()
            source.parent.rmdir()
            self.assertEqual(owners.main(["recover", "--json"]), 0)
            self.assertEqual(owners.main(["recover", "--json"]), 0)
        saved = owners.read_object(self.root / "state/recovered.json", [])
        records = [owners.object_fields(item) for item in owners.list_items(saved.get("workers"))]
        self.assertEqual([item.get("thread_id") for item in records], ["lost-thread"])
        current = self.collect(observed.entries)
        self.assertEqual([(worker.thread_id, worker.classification, worker.director) for worker in current.workers],
                         [("lost-thread", owners.WorkerClass.OwnedLive, "director-one")])
        absent = self.collect([self.director_entry()])
        with patch.object(owners, "collect_sources", return_value=absent), redirect_stdout(io.StringIO()):
            self.assertEqual(owners.main(["recover", "--json"]), 0)
        self.assertEqual(self.collect(observed.entries).workers[0].classification, owners.WorkerClass.OwnedLive)

    def test_already_ended_route_is_kept_without_an_end_call_or_success_claim(self) -> None:
        worker = self.worker()
        self.write_json(str(Path(worker.run) / "mesh_roster.json"), {
            worker.name: {"thread_id": worker.thread_id, "status": "ended"},
        })
        sources = owners.OwnershipSources([worker], [], [], [])
        state = self.root / "ended-state"
        with patch.object(owners, "collect_sources", return_value=sources), \
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as command:
            first = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
            preview = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state)
            direct = owners.mesh_end(worker)
            result = owners.end_workers(sources, owners.WorkerClass.DirectorGone, state, apply=True)
        command.assert_not_called()
        self.assertTrue(first.first_run)
        self.assertIsInstance(direct, owners.WorkerKept)
        self.assertEqual((result.ended, result.failed), ((), ()))
        self.assertIn(worker.name + ": would keep: mesh route already ended", preview.evidence)
        self.assertIn(worker.name + ": kept: mesh route already ended", result.evidence)
        self.assertNotEqual(owners.WorkerTurnState.from_status("ended"), owners.WorkerTurnState.Idle)

    def test_applied_end_logs_only_workers_in_the_selected_class(self) -> None:
        gone = self.worker(name="gone", thread="gone-thread", director="departed", unit="departed-unit")
        live = self.worker(name="live", thread="live-thread", classification="OwnedLive")
        unknown = self.worker(name="unknown", thread="unknown-thread", director="", unit="")
        sources = owners.OwnershipSources([gone, live, unknown],
                                         [owners.Director("director-one", "unit-director", "hierarchies")], [], [])
        state = self.root / "selected-log"
        with patch.object(subprocess, "run") as command:
            first = owners.end_workers(sources, owners.WorkerClass.OwnerUnknown, state, apply=True)
            second = owners.end_workers(sources, owners.WorkerClass.OwnerUnknown, state, apply=True)
        command.assert_not_called()
        self.assertTrue(first.first_run)
        self.assertEqual(second.failed, ())
        records = [owners.object_fields(cast(object, json.loads(line)))
                   for line in (state / "ends.jsonl").read_text().splitlines()]
        self.assertEqual([owners.object_fields(item.get("worker")).get("thread_id") for item in records],
                         ["unknown-thread", "unknown-thread"])
        self.assertTrue(all("kept:" in str(item.get("outcome")) for item in records))

    def test_message_address_needs_a_director_and_an_unambiguous_worker_route(self) -> None:
        for root in ("unit", "delegate"):
            self.write_json(root + "/duplicate/mesh_roster.json", {
                "same-name": {"thread_id": root + "-thread", "status": "paused"},
            })
        routes = [owners.OwnershipEvidence("same-name", root + "-thread", str(self.root / root / "duplicate"),
                                          owners.MissingDirectorRecord(), ()) for root in ("unit", "delegate")]
        directors = [owners.Director("director-one", "unit-director", "hierarchies")]
        self.assertEqual(owners.message_ownership_records(self.message_record("worker:duplicate/same-name"),
                                                         "message log", directors, routes), [])
        self.assertEqual(owners.message_ownership_records(self.message_record("worker:duplicate/same-name", "peer-worker"),
                                                         "message log", directors, routes[:1]), [])
        absolute = "worker:" + str(self.root / "unit/duplicate") + "/same-name"
        recovered = owners.message_ownership_records(self.message_record(absolute, "session:director-one"),
                                                      "message log", directors, routes)
        self.assertEqual([(item.thread_id, item.owner) for item in recovered], [("unit-thread", directors[0])])

    def test_freestanding_shutdown_snapshot_without_worker_relation_is_excluded(self) -> None:
        self.write_json("shutdown/chat.json", {
            "session": {"kind": "freestanding", "session_id": "chat-thread", "name": "interactive-chat"},
            "thread_id": "chat-thread", "cwd": "/work/chat",
        })
        daemon = replace(self.unknown_entry("chat-thread", "interactive-chat"), role="freestanding", cwd="/work/chat")
        self.assertEqual(self.collect([daemon]).workers, [])

    def test_real_message_address_recovers_only_its_named_worker(self) -> None:
        self.write_json("unit/message-run/mesh_roster.json", {
            "recipient": {"thread_id": "recipient-thread", "status": "paused"},
            "other": {"thread_id": "other-thread", "status": "paused"},
        })
        self.write_text("messages.jsonl", json.dumps(self.message_record("worker:message-run/recipient")) + "\n")
        sources = owners.recover_sources(self.collect([self.director_entry()]))
        workers = {worker.name: worker for worker in sources.workers}
        self.assertEqual(workers["recipient"].classification, owners.WorkerClass.OwnedLive)
        self.assertEqual(workers["recipient"].director, "director-one")
        self.assertEqual(workers["other"].classification, owners.WorkerClass.OwnerUnknown)

    def test_conflict_report_names_live_and_departed_candidate_owners(self) -> None:
        for run, director in (("a-live", "director-one"), ("z-stale", "departed-director")):
            self.write_text("unit/" + run + "/director", director + "\n")
            self.write_json("unit/" + run + "/mesh_roster.json", {
                "legacy-worker": {"thread_id": "shared-thread", "status": "paused"},
            })
        output = io.StringIO()
        observed = roster.Roster([self.director_entry()], [])
        with patch.object(roster, "roster", return_value=observed), redirect_stdout(output):
            code = owners.main(["report", "--json"])
        self.assertEqual(code, 0)
        report = cast(dict[str, object], json.loads(output.getvalue()))
        workers = cast(list[dict[str, object]], report["workers"])
        ownership = cast(dict[str, object], workers[0].get("ownership", {}))
        self.assertEqual(ownership.get("state"), "ConflictingOwners")
        candidates = cast(list[dict[str, object]], ownership.get("candidates", []))
        self.assertEqual({(item["session_id"], item["live"]) for item in candidates},
                         {("director-one", True), ("departed-director", False)})


if __name__ == "__main__":
    _ = unittest.main()
