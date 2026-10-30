"""Unit-owned worker resolution against an isolated real mesh protocol server."""

from __future__ import annotations

import fcntl
import io
import json
import os
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import BinaryIO, Literal, TextIO, cast, override
from unittest.mock import patch

from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.direct import progress_history, review_pool, workers
from scripts.direct.recorder_fixture import invoke_recorder


SCRIPT = Path(__file__).with_name("workers.py")


class AdoptionWriteInterrupted(BaseException):
    """A simulated process loss after a durable transaction write."""


@contextmanager
def bounded_worker_change() -> Generator[None, None, None]:
    """A lock-order regression fails instead of hanging the fixture forever."""
    def expired(number: int, frame: object) -> None:
        raise AssertionError(f"worker change exceeded its 15s deadline ({number}, {type(frame).__name__})")

    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, 15)
    try:
        yield
    finally:
        _ = signal.setitimer(signal.ITIMER_REAL, *timer)
        _ = signal.signal(signal.SIGALRM, previous)


@contextmanager
def observe_adoption_writes(source: Path, destination: Path,
                            observer: Callable[[str], None]) -> Generator[None, None, None]:
    """Inject a crash after real durable writes, without a production-only hook."""
    atomic = workers._atomic  # pyright: ignore[reportPrivateUsage]
    write_roster = workers._write_roster  # pyright: ignore[reportPrivateUsage]
    ledger_path = workers.ledger_for_run(destination).path
    observed: set[str] = set()

    def report(stage: str) -> None:
        if stage not in observed:
            observed.add(stage)
            observer(stage)

    def persisted(path: Path, data: Mapping[str, object]) -> None:
        atomic(path, data)
        if path != ledger_path:
            return
        if data.get("adoptions"):
            report("ledger" if "destination" in observed else "intent")
        elif "ledger" in observed:
            report("complete")

    def roster_written(handle: TextIO, roster: dict[str, object]) -> None:
        write_roster(handle, roster)
        path = Path(cast(str, handle.name))
        if path == source / codex_mesh.ROSTER_FILE:
            report("source")
        elif path == destination / codex_mesh.ROSTER_FILE:
            report("destination")

    with (patch.object(workers, "_atomic", new=persisted),
          patch.object(workers, "_write_roster", new=roster_written)):
        yield


def setUpModule() -> None:
    # Reuse the synthetic sign-in and disposable HOME, never the real ledger.
    mesh_fixture.setUpModule()


def json_object(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    # Opening a taking roster under flock can precede a refused adoption.
    # Empty ledger/state files still fail decoding rather than being hidden.
    if path.name == codex_mesh.ROSTER_FILE and not content.strip():
        return {}
    value = cast(object, json.loads(content))
    if not isinstance(value, dict):
        raise AssertionError(f"expected an object in {path}")
    return cast(dict[str, object], value)


class WorkerResolutionTests(mesh_fixture.MeshCommandFixture):
    def test_ledger_uses_canonical_production_for_both_headers_and_saved_metadata(self) -> None:
        worktree = self.session_dir.parent / "repository"
        plan = worktree / "plan.md"
        production_doc = worktree / "production.md"
        _ = production_doc.write_text("## Production Context\n\n- **Formerly:** `demo`\n")
        notifier = Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-renamed"
        notifier.mkdir(parents=True)
        _ = (notifier / "conf").write_text(
            "TARGET=session:fixture\nCHECK="
            + shlex.join(["zsh", "/opt/tools/production_check.sh", str(production_doc)]) + "\n")
        for source in ("**Production: demo**", "**Production:** demo", "saved metadata"):
            with self.subTest(source=source):
                self.write_run_context(self.session_dir)
                state = json_object(self.session_dir / workers.STATE)
                if source == "saved metadata":
                    state.update({"production": "demo", "unit": "builders"})
                else:
                    _ = plan.write_text(f'> {source} — unit `builders`; plan\n')
                    state["plan_doc"] = str(plan)
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.assertEqual(progress_history.resolve_production_unit(state),
                                 progress_history.ProductionUnitIdentity("demo", "builders"))
                self.assertEqual(workers.ledger_for_run(self.session_dir).path,
                                 workers.ledger_path("renamed", "builders"))

    def test_plan_header_metadata_resolves_without_completing_a_pending_worker_move(self) -> None:
        state = json_object(self.session_dir / workers.STATE)
        worktree = Path(str(state["working_dir"]))
        plan = worktree / "unit-plan.md"
        _ = plan.write_text('> **Production: fixture-production** — unit `fixture-unit`; production doc `production.md`\n')
        state["project_plan_doc"] = plan.name
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        # A read-only metadata caller must not recover even an invalid pending
        # migration; recovery is exclusively ledger_for_run's responsibility.
        journal = workers.ledger_path("fixture-production", "fixture-unit").with_suffix(".move.json")
        journal.parent.mkdir(parents=True, exist_ok=True)
        _ = journal.write_text('{"pending": "do not migrate during a hook"}')
        before = {path: path.read_bytes() for path in (journal, plan, self.session_dir / workers.STATE)}
        identity = progress_history.resolve_production_unit(state)
        self.assertEqual(identity, progress_history.ProductionUnitIdentity("fixture-production", "fixture-unit"))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        journal.unlink()
        self.assertEqual(workers.ledger_for_run(self.session_dir).path,
                         workers.ledger_path("fixture-production", "fixture-unit"))

    @override
    def setUp(self) -> None:
        super().setUp()
        working = self.session_dir.parent / "repository"
        working.mkdir()
        self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_WORKERS_STATE": str(self.session_dir.parent / "unit-workers"),
            "NOTIFIER_STATE_DIR": str(self.session_dir.parent / "notifier"),
        }))
        self.write_run_context(self.session_dir)

    def write_run_context(self, run: Path) -> None:
        _ = (run / "working_dir").write_text(str(self.session_dir.parent / "repository"))
        _ = (run / "progress_history_state.json").write_text(json.dumps({
            "status": "active", "working_dir": str(self.session_dir.parent / "repository"),
            "run_id": run.name,
        }))

    def next_run(self, name: str) -> Path:
        run = self.session_dir.parent / name
        run.mkdir()
        self.write_run_context(run)
        _ = (run / codex_mesh.SERVER_FILE).write_bytes(
            (self.session_dir / codex_mesh.SERVER_FILE).read_bytes())
        return run

    def resolve(self, run: Path, role: str = "impl", *, working_dir: str = "") -> str:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "resolve", "--session-dir", str(run), "--role", role,
             "--owner-pid", str(os.getpid()), *(["--working-dir", working_dir] if working_dir else [])],
            cwd=self.session_dir.parent / "repository", env=os.environ.copy(),
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1, result.stdout)
        return result.stdout.strip()

    def open_worker(self, name: str = "address-with-no-role-suffix", role: str = "impl") -> None:
        self.write_pool_roster({name: {
            "thread_id": mesh_fixture.THREAD_ID, "status": "done", "role": role,
            "model": "gpt-kept-sol", "effort": "xhigh", "family": "codex",
            "codex_home": os.environ["CODEX_HOME"], "port": self.server.port,
            "cwd": str(self.session_dir.parent / "repository"),
        }})

    def complete_assignment(self, name: str, *, role: Literal["impl", "test", "fix", "review"] = "impl") -> None:
        """Consume any dispatch reservation through a real, completed turn."""
        args = self.start_args()
        args.to = name
        args.role = role
        args.message_file = str(self.session_dir / "prompt.txt")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)
        self.assertEqual(code, 0, errors.getvalue())

    def configure_new_worker(self) -> None:
        """Resolve through a temporary registry and allocate a distinct real thread."""
        original = self.server.respond

        @mesh_fixture.bounded_fixture
        def open_second(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/start":
                return [{"id": request["id"], "result": {"thread": {"id": "second-thread"}}}]
            frames = original(request)
            params = cast(dict[str, object], request.get("params", {}))
            if frames is not None and params.get("threadId") == "second-thread":
                return cast(list[dict[str, object]], json.loads(
                    json.dumps(frames).replace(mesh_fixture.THREAD_ID, "second-thread")))
            return frames

        self.server.respond = open_second
        registry = self.session_dir.parent / "registry.conf"
        _ = registry.write_text("[codex.agents]\ngpt-kept-sol=high,xhigh,max\n" +
                                "[assignments]\ndirect=codex\n[direct.codex]\n" +
                                "impl=gpt-kept-sol:xhigh\n")
        checkpoint = self.session_dir.parent / "catalog-current"
        checkpoint.touch()
        self.patches.enter_context(patch.dict(os.environ, {
            "AGENTS_CONFIG_FILE": str(registry),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(checkpoint),
        }))

    def lifecycle_worker(self, name: str = "w7") -> workers.UnitWorkerLedger:
        state = json_object(self.session_dir / workers.STATE)
        state.update({"production": "before", "unit": "builders"})
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        self.open_worker(name)
        store = workers.ledger_for_run(self.session_dir)
        with store.locked() as ledger:
            ledger.workers[name] = workers.UnitWorker(
                name, "impl", "codex", "gpt-kept-sol", "xhigh", {"kind": "WorkerLaunchPolicy"},
                workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]),
                workers.HeldBy(str(self.session_dir)),
                [workers.WorkerEvent("opened", "lifecycle fixture", str(self.session_dir), 1_793_188_800.0, {})],
            )
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster[name])["unit_ledger"] = str(store.path)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        return store

    def leased_pool_reviewer(self, *, busy: bool = False) -> tuple[Path, Path]:
        state = json_object(self.session_dir / workers.STATE)
        state.update({"production": "production", "unit": "builders-unit"})
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state), encoding="utf-8")
        pool_root = self.session_dir.parent / "review-pool"
        self.patches.enter_context(patch.dict(os.environ, {"REVIEW_POOL_STATE": str(pool_root)}))
        pool = pool_root / "production" / "pool.json"
        source = pool.parent / "mesh" / "0"
        source.mkdir(parents=True)
        _ = (source / codex_mesh.SERVER_FILE).write_bytes(
            (self.session_dir / codex_mesh.SERVER_FILE).read_bytes())
        instructions = source / "code.md"
        _ = instructions.write_text("Review only; keep the thread's context.\n", encoding="utf-8")
        _ = (source / codex_mesh.ROSTER_FILE).write_text(json.dumps({"code6": {
            "thread_id": mesh_fixture.THREAD_ID, "status": "done", "role": "review",
            "model": "gpt-kept-sol", "effort": "xhigh", "family": "codex",
            "codex_home": os.environ["CODEX_HOME"], "port": self.server.port,
            "cwd": str(self.session_dir.parent / "repository"),
            "instructions_file": str(instructions), "access_policy": {"kind": "ReadOnlyReviewer"},
        }}), encoding="utf-8")
        _ = pool.write_text(json.dumps({"generation": 0, "templates": {}, "reviewers": {
            "code6": {"type": "code", "thread_id": mesh_fixture.THREAD_ID,
                      "prime_hash": "kept-prompt", "lease": {"unit": "builders-unit", "ordinal": 6},
                      "busy_pid": os.getpid() if busy else None, "reviews": 6},
        }}), encoding="utf-8")
        with review_pool.ProductionPool(pool.parent).locked():
            pass
        return pool, source

    def test_first_review_adopts_the_leased_thread_before_opening_an_ordinary_worker(self) -> None:
        pool, _ = self.leased_pool_reviewer()
        first = self.resolve(self.session_dir, "review")
        self.assertEqual(first, "repository-review")
        self.assertEqual(json_object(pool)["reviewers"], {})
        self.assertEqual(cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)[first])["thread_id"],
                         mesh_fixture.THREAD_ID)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("thread/fork"), [])
        self.complete_assignment(first, role="review")
        before = pool.read_bytes()
        self.assertEqual(self.resolve(self.session_dir, "review"), first)
        self.assertEqual(pool.read_bytes(), before)
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_busy_leased_review_reserves_the_first_slot_until_it_can_be_adopted(self) -> None:
        pool, source = self.leased_pool_reviewer(busy=True)
        self.configure_new_worker()
        registry = self.session_dir.parent / "registry.conf"
        with registry.open("a", encoding="utf-8") as handle:
            _ = handle.write("review=gpt-kept-sol:xhigh\n")
        before = pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()
        second = self.resolve(self.session_dir, "review")
        self.assertEqual(second, "repository-review2")
        self.assertEqual((pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()), before)
        self.complete_assignment(second, role="review")
        data = json_object(pool)
        cast(dict[str, object], cast(dict[str, object], data["reviewers"])["code6"])["busy_pid"] = None
        _ = pool.write_text(json.dumps(data))
        self.assertEqual(self.resolve(self.session_dir, "review"), "repository-review")
        self.assertEqual(json_object(pool)["reviewers"], {})
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertEqual(ledger.workers["repository-review"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
            self.assertEqual(set(ledger.workers), {"repository-review", "repository-review2"})
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.methods("thread/fork"), [])

    def assert_invalid_transfer_inbox_is_preserved(self, *, recovery: bool, source_queue: bool = False) -> None:
        pool, source = self.leased_pool_reviewer()
        giving = source / "code6.pending.json"
        target = self.session_dir / "repository-review.pending.json"
        _ = giving.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                                          "messages": ["keep this request"]}))
        _ = target.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                                         "messages": ["already waiting"]}))
        if recovery:
            atomic = workers._atomic  # pyright: ignore[reportPrivateUsage]

            def interrupted(path: Path, data: Mapping[str, object]) -> None:
                atomic(path, data)
                if "worker-transfers" in path.parts:
                    raise AdoptionWriteInterrupted()

            with patch.object(workers, "_atomic", new=interrupted), self.assertRaises(AdoptionWriteInterrupted):
                _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        invalid = giving if source_queue else target
        _ = invalid.write_text('{"thread_id": "kept-thread", "messages": 42}' if recovery
                               else '["a queue must be an object"]')
        before = {path: path.read_bytes() for path in (giving, target) if path.exists()}
        with self.assertRaises(workers.WorkerUnavailable) as refused:
            _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertIn(str(invalid), str(refused.exception))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertIn("code6", cast(dict[str, object], json_object(pool)["reviewers"]))

    def test_first_transfer_refuses_a_wrong_shape_destination_without_changing_either_inbox(self) -> None:
        self.assert_invalid_transfer_inbox_is_preserved(recovery=False)

    def test_recovered_transfer_refuses_a_wrong_shape_destination_without_changing_either_inbox(self) -> None:
        self.assert_invalid_transfer_inbox_is_preserved(recovery=True)

    def test_transfer_refuses_a_wrong_shape_source_and_names_its_inbox(self) -> None:
        self.assert_invalid_transfer_inbox_is_preserved(recovery=False, source_queue=True)

    def test_migrated_reviewer_resumes_delivers_queue_and_restarts_without_reading_the_pool_archive(self) -> None:
        pool, source = self.leased_pool_reviewer()
        _ = (source / "code6.pending.json").write_text(json.dumps({
            "thread_id": mesh_fixture.THREAD_ID, "messages": ["queued before the move"]}))
        _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        archive = pool.parent.parent
        original_open = Path.open

        def refuse_archive(path: Path, mode: str = "r", buffering: int = -1,
                           encoding: str | None = None, errors: str | None = None,
                           newline: str | None = None) -> TextIO | BinaryIO:
            if path.is_relative_to(archive):
                raise AssertionError(f"moved worker read the archive: {path}")
            return cast(TextIO | BinaryIO, original_open(path, mode, buffering, encoding, errors, newline))

        with patch.object(Path, "open", new=refuse_archive):
            self.assertNotIn(str(source), codex_mesh.worker_run_history(str(self.session_dir), "repository-review"))
            self.assertFalse(codex_mesh.DetachedRunWatch(str(self.session_dir), "repository-review").current().live)
            self.complete_assignment("repository-review", role="review")
            inputs = [item["text"] for request in self.methods("turn/start")
                      for item in cast(list[dict[str, object]], cast(dict[str, object], request["params"])["input"])]
            self.assertIn("queued before the move", inputs)
            workers.pause_unit("production", "builders-unit")
            restarted = self.next_run("reviewer-restarted")
            state = json_object(restarted / workers.STATE)
            state.update({"production": "production", "unit": "builders-unit"})
            _ = (restarted / workers.STATE).write_text(json.dumps(state))
            self.assertEqual(workers.resolve(restarted, "review"), "repository-review")
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("thread/fork"), [])

    def test_pool_reviewer_moves_to_the_unit_on_its_existing_thread_and_rerun_changes_nothing(self) -> None:
        pool, source = self.leased_pool_reviewer()
        results = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(results, (workers.ReviewerMigrated(
            "code6", "builders-unit", "repository-review", mesh_fixture.THREAD_ID),))
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            reviewer = ledger.workers["repository-review"]
            self.assertEqual(reviewer.role, "review")
            self.assertEqual(reviewer.backend, workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
            self.assertEqual(reviewer.ownership, workers.HeldBy(str(self.session_dir)))
            self.assertEqual(reviewer.access_policy.get("kind"), "ReadOnlyReviewer")
        destination = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        record = cast(dict[str, object], destination["repository-review"])
        self.assertEqual(record["thread_id"], mesh_fixture.THREAD_ID)
        self.assertEqual(record["role"], "review")
        self.assertEqual(record["model"], "gpt-kept-sol")
        self.assertEqual(Path(cast(str, record["instructions_file"])).read_text(encoding="utf-8"),
                         "Review only; keep the thread's context.\n")
        self.assertNotIn("code6", cast(dict[str, object], json_object(pool)["reviewers"]))
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("thread/fork"), [])
        ledger_path = workers.ledger_for_run(self.session_dir).path
        paths = (pool, ledger_path, source / codex_mesh.ROSTER_FILE,
                 self.session_dir / codex_mesh.ROSTER_FILE)
        before = {path: path.read_bytes() for path in paths}
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), ())
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def assert_reviewer_resolves_after_unit_move_without_archive(self, change: Literal["rename", "move"]) -> None:
        pool, _ = self.leased_pool_reviewer()
        _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.complete_assignment("repository-review", role="review")
        archive = pool.parent.parent
        original_open = Path.open

        def refuse_archive(path: Path, mode: str = "r", buffering: int = -1,
                           encoding: str | None = None, errors: str | None = None,
                           newline: str | None = None) -> TextIO | BinaryIO:
            if path.is_relative_to(archive):
                raise AssertionError(f"moved reviewer opened the archive: {path}")
            return cast(TextIO | BinaryIO, original_open(path, mode, buffering, encoding, errors, newline))

        with bounded_worker_change(), patch.object(Path, "open", new=refuse_archive), \
                patch.object(workers, "transfer_reviewers", side_effect=AssertionError("adoption ran again")):
            if change == "rename":
                workers.rename_unit("production", "builders-unit", "renamed-unit", runs=(self.session_dir,))
                expected = workers.ledger_path("production", "renamed-unit")
            else:
                workers.move_unit("production", "another-production", "builders-unit", runs=(self.session_dir,))
                expected = workers.ledger_path("another-production", "builders-unit")
            self.assertEqual(workers.ledger_for_run(self.session_dir).path, expected)
            self.assertEqual(workers.resolve(self.session_dir, "review"), "repository-review")
        record = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["repository-review"])
        self.assertEqual(record["thread_id"], mesh_fixture.THREAD_ID)
        self.assertEqual(record["unit_ledger"], str(expected))
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("thread/fork"), [])

    def test_a_renamed_units_adopted_reviewer_resolves_without_reading_the_archive(self) -> None:
        self.assert_reviewer_resolves_after_unit_move_without_archive("rename")

    def test_a_moved_units_adopted_reviewer_resolves_without_reading_the_archive(self) -> None:
        self.assert_reviewer_resolves_after_unit_move_without_archive("move")

    def test_busy_pool_reviewer_is_deferred_then_moves_after_release(self) -> None:
        pool, source = self.leased_pool_reviewer(busy=True)
        before = pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()
        result, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        assert isinstance(result, workers.ReviewerDeferred), result
        self.assertEqual((result.reviewer, result.unit), ("code6", "builders-unit"))
        self.assertTrue(result.reason)
        self.assertEqual((pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()), before)
        self.assertFalse(workers.ledger_for_run(self.session_dir).path.exists())
        data = json_object(pool)
        cast(dict[str, object], cast(dict[str, object], data["reviewers"])["code6"])["busy_pid"] = None
        _ = pool.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False),
                         (workers.ReviewerMigrated("code6", "builders-unit", "repository-review", mesh_fixture.THREAD_ID),))

    def test_a_different_unit_reviewer_releases_the_lease_without_replacing_either_thread(self) -> None:
        pool, source = self.leased_pool_reviewer()
        self.open_worker("repository-review", "review")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["repository-review"])["thread_id"] = "unit-owned-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster), encoding="utf-8")
        store = workers.ledger_for_run(self.session_dir)
        with store.locked() as ledger:
            ledger.workers["repository-review"] = workers.UnitWorker(
                "repository-review", "review", "codex", "gpt-kept-sol", "xhigh", {"kind": "ReadOnlyReviewer"},
                workers.CodexThread("unit-owned-thread", os.environ["CODEX_HOME"]),
                workers.HeldBy(str(self.session_dir)), [])
        paths = (source / codex_mesh.ROSTER_FILE, self.session_dir / codex_mesh.ROSTER_FILE)
        before = {path: path.read_bytes() for path in paths}
        result, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(result, workers.ReviewerReleased("code6", "builders-unit"))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        with review_pool.ProductionPool(pool.parent).locked(write=False) as state:
            self.assertIsInstance(state.reviewers["code6"].lease, review_pool.Unleased)
        with store.locked() as ledger:
            self.assertTrue(ledger.reviewers_adopted)
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), ())

    def test_transfer_dry_run_reports_the_move_without_writing_pool_or_ledger(self) -> None:
        pool, source = self.leased_pool_reviewer()
        data = json_object(pool)
        reviewers = cast(dict[str, object], data["reviewers"])
        reviewers["ui2"] = {"type": "ui", "thread_id": "ui-thread", "prime_hash": "ui-prompt", "reviews": 1,
                            "busy_pid": None, "lease": {"unit": "builders-unit", "ordinal": 1}}
        reviewers["code9"] = {"type": "code", "thread_id": "unleased-thread", "prime_hash": "code-prompt", "reviews": 0,
                              "busy_pid": None, "lease": None}
        _ = pool.write_text(json.dumps(data))
        store = workers.ledger_for_run(self.session_dir)
        with store.locked():
            pass
        paths = (pool, store.path, source / codex_mesh.ROSTER_FILE)
        before = {path: path.read_bytes() for path in paths}
        files_before = {path.relative_to(self.session_dir.parent) for path in self.session_dir.parent.rglob("*")}
        output = io.StringIO()
        with patch.object(sys, "argv", [str(SCRIPT), "transfer-reviewers", "--production", "production",
                                        "--session-dir", str(self.session_dir)]), redirect_stdout(output):
            self.assertEqual(workers.main(), 0)
        self.assertEqual(output.getvalue(),
                         "skipped: code9: reviewer has no unit lease\n"
                         + "skipped: ui2: UI reviewer stays outside code review\n"
                         + "preview: would migrate: builders-unit: code6 -> repository-review "
                         + f"(owner {self.session_dir}; thread {mesh_fixture.THREAD_ID} checked)\n"
                         + f"Archive after each completed move: {pool.parent.parent}; the moved worker does not read it.\n")
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual({path.relative_to(self.session_dir.parent) for path in self.session_dir.parent.rglob("*")},
                         files_before)

    def transfer_cli(self, *, apply: bool) -> tuple[int, str, str]:
        arguments = [str(SCRIPT), "transfer-reviewers", "--production", "production",
                     "--session-dir", str(self.session_dir)]
        if apply:
            arguments.append("--apply")
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", arguments), redirect_stdout(output), redirect_stderr(errors):
            code = workers.main()
        return code, output.getvalue(), errors.getvalue()

    def test_transfer_without_a_pool_reports_no_reviewers_and_writes_nothing(self) -> None:
        pool_root = self.session_dir.parent / "absent-pool"
        before = {path: path.read_bytes() for path in self.session_dir.parent.rglob("*") if path.is_file()}
        paths = set(self.session_dir.parent.rglob("*"))
        with patch.dict(os.environ, {"REVIEW_POOL_STATE": str(pool_root)}):
            for apply in (False, True):
                with self.subTest(apply=apply):
                    code, output, errors = self.transfer_cli(apply=apply)
                    self.assertEqual(code, 0, errors)
                    self.assertEqual(output, "No reviewer pool for production; nothing to move.\n")
                    self.assertEqual(errors, "")
                    self.assertEqual(set(self.session_dir.parent.rglob("*")), paths)
                    self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_applied_transfer_reports_checked_owner_and_thread_and_lists_skipped_reviewers(self) -> None:
        pool, _ = self.leased_pool_reviewer()
        data = json_object(pool)
        reviewers = cast(dict[str, object], data["reviewers"])
        reviewers["ui2"] = {"type": "ui", "thread_id": "ui-thread", "prime_hash": "ui-prompt", "reviews": 1,
                            "busy_pid": None, "lease": {"unit": "builders-unit", "ordinal": 1}}
        reviewers["code9"] = {"type": "code", "thread_id": "unleased-thread", "prime_hash": "code-prompt", "reviews": 0,
                              "busy_pid": None, "lease": None}
        _ = pool.write_text(json.dumps(data))
        code, output, errors = self.transfer_cli(apply=True)
        self.assertEqual(code, 0, errors)
        self.assertIn("migrated:", output)
        self.assertNotIn("preview:", output.lower())
        self.assertIn(str(self.session_dir), output)
        self.assertIn(mesh_fixture.THREAD_ID, output)
        self.assertIn("checked", output)
        self.assertIn("skipped: ui2", output)
        self.assertIn("skipped: code9", output)
        self.assertIn(str(pool.parent.parent), output)
        self.assertIn("does not read", output)

    def test_applied_transfer_returns_failure_for_a_busy_reviewer_but_preview_succeeds(self) -> None:
        pool, source = self.leased_pool_reviewer(busy=True)
        before = pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()
        preview, output, errors = self.transfer_cli(apply=False)
        self.assertEqual(preview, 0, errors)
        self.assertIn("preview:", output.lower())
        self.assertIn("deferred:", output)
        applied, output, errors = self.transfer_cli(apply=True)
        self.assertNotEqual(applied, 0, errors)
        self.assertIn("deferred:", output)
        self.assertIn(f"owner {self.session_dir}; thread {mesh_fixture.THREAD_ID} checked", output)
        self.assertEqual((pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()), before)

    def test_applied_transfer_returns_failure_for_a_destination_conflict(self) -> None:
        pool, source = self.leased_pool_reviewer()
        state = json_object(self.session_dir / workers.STATE)
        state["unit"] = "another-unit"
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        before = pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()
        code, output, errors = self.transfer_cli(apply=True)
        self.assertNotEqual(code, 0, errors)
        self.assertIn("conflict:", output)
        self.assertIn(f"owner not named; thread {mesh_fixture.THREAD_ID} checked", output)
        self.assertEqual((pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()), before)

    def test_unfenced_transfer_replans_after_the_pool_uses_its_thread_again(self) -> None:
        pool, source = self.leased_pool_reviewer()
        atomic = workers._atomic  # pyright: ignore[reportPrivateUsage]

        def interrupted(path: Path, data: Mapping[str, object]) -> None:
            atomic(path, data)
            if "worker-transfers" in path.parts:
                raise AdoptionWriteInterrupted()

        with patch.object(workers, "_atomic", new=interrupted), self.assertRaises(AdoptionWriteInterrupted):
            _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        roster = json_object(source / codex_mesh.ROSTER_FILE)
        record = cast(dict[str, object], roster["code6"])
        record.update({"status": "running", "turn_id": "later-pool-turn"})
        _ = (source / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        result, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertIsInstance(result, workers.ReviewerDeferred)
        record["status"] = "done"
        _ = (source / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False),
                         (workers.ReviewerMigrated("code6", "builders-unit", "repository-review", mesh_fixture.THREAD_ID),))
        self.assertEqual(json_object(pool)["reviewers"], {})
        self.assertEqual(self.methods("thread/start"), [])

    def test_fenced_transfer_uses_the_next_name_when_its_saved_name_is_claimed(self) -> None:
        pool, source = self.leased_pool_reviewer()
        write_roster = workers._write_roster  # pyright: ignore[reportPrivateUsage]

        def interrupted(handle: TextIO, data: dict[str, object]) -> None:
            write_roster(handle, data)
            if Path(cast(str, handle.name)) == source / codex_mesh.ROSTER_FILE:
                raise AdoptionWriteInterrupted()

        with patch.object(workers, "_write_roster", new=interrupted), self.assertRaises(AdoptionWriteInterrupted):
            _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.open_worker("repository-review", "impl")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["repository-review"])["thread_id"] = "competing-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False),
                         (workers.ReviewerMigrated("code6", "builders-unit", "repository-review2", mesh_fixture.THREAD_ID),))
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        self.assertEqual(cast(dict[str, object], roster["repository-review"])["thread_id"], "competing-thread")
        self.assertEqual(cast(dict[str, object], roster["repository-review2"])["slot"], "review2")
        self.assertEqual(json_object(pool)["reviewers"], {})

    def test_retargeted_transfer_keeps_an_inbox_moved_before_registration_stopped(self) -> None:
        pool, source = self.leased_pool_reviewer()
        _ = (source / "code6.pending.json").write_text(json.dumps({
            "thread_id": mesh_fixture.THREAD_ID, "messages": ["review this change"]}))
        write_roster = workers._write_roster  # pyright: ignore[reportPrivateUsage]

        def interrupted(handle: TextIO, data: dict[str, object]) -> None:
            if Path(cast(str, handle.name)) == self.session_dir / codex_mesh.ROSTER_FILE:
                raise AdoptionWriteInterrupted()
            write_roster(handle, data)

        with patch.object(workers, "_write_roster", new=interrupted), self.assertRaises(AdoptionWriteInterrupted):
            _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.open_worker("repository-review", "impl")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["repository-review"])["thread_id"] = "competing-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        result, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(result, workers.ReviewerMigrated("code6", "builders-unit", "repository-review2", mesh_fixture.THREAD_ID))
        self.assertEqual(json_object(self.session_dir / "repository-review2.pending.json")["messages"], ["review this change"])
        self.assertEqual(json_object(pool)["reviewers"], {})

    def prepare_retargeted_inboxes(self, replacement_messages: list[str]) -> tuple[Path, Path, Path]:
        pool, source = self.leased_pool_reviewer()
        _ = (source / "code6.pending.json").write_text(json.dumps({
            "thread_id": mesh_fixture.THREAD_ID,
            "messages": ["review this change", "review this change"]}))
        write_roster = workers._write_roster  # pyright: ignore[reportPrivateUsage]

        def interrupted(handle: TextIO, data: dict[str, object]) -> None:
            if Path(cast(str, handle.name)) == self.session_dir / codex_mesh.ROSTER_FILE:
                raise AdoptionWriteInterrupted()
            write_roster(handle, data)

        with patch.object(workers, "_write_roster", new=interrupted), self.assertRaises(AdoptionWriteInterrupted):
            _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.open_worker("repository-review", "impl")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["repository-review"])["thread_id"] = "competing-thread"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        old_inbox = self.session_dir / "repository-review.pending.json"
        target_inbox = self.session_dir / "repository-review2.pending.json"
        content = json.dumps({"thread_id": mesh_fixture.THREAD_ID, "messages": replacement_messages}) if replacement_messages else ""
        _ = target_inbox.write_text(content)
        return pool, old_inbox, target_inbox

    def test_retargeted_transfer_moves_messages_into_an_existing_empty_inbox(self) -> None:
        pool, old_inbox, target_inbox = self.prepare_retargeted_inboxes([])
        result, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(result, workers.ReviewerMigrated("code6", "builders-unit", "repository-review2", mesh_fixture.THREAD_ID))
        self.assertEqual(json_object(target_inbox)["messages"], ["review this change", "review this change"])
        self.assertEqual(json_object(old_inbox)["messages"], [])
        self.assertFalse(json_object(old_inbox)["accepting_messages"])
        self.assertEqual(json_object(pool)["reviewers"], {})
        before = old_inbox.read_bytes(), target_inbox.read_bytes(), pool.read_bytes()
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), ())
        self.assertEqual((old_inbox.read_bytes(), target_inbox.read_bytes(), pool.read_bytes()), before)

    def test_retargeted_transfer_merges_existing_messages_once_after_an_interrupted_write(self) -> None:
        pool, old_inbox, target_inbox = self.prepare_retargeted_inboxes(["already queued"])
        write_inbox = workers._write_inbox  # pyright: ignore[reportPrivateUsage]

        def interrupted(handle: TextIO, data: Mapping[str, object]) -> None:
            write_inbox(handle, data)
            if Path(cast(str, handle.name)) == target_inbox:
                raise AdoptionWriteInterrupted()

        with patch.object(workers, "_write_inbox", new=interrupted), self.assertRaises(AdoptionWriteInterrupted):
            _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        expected = ["review this change", "review this change", "already queued"]
        self.assertEqual(json_object(target_inbox)["messages"], expected)
        self.assertEqual(json_object(old_inbox)["messages"], ["review this change", "review this change"])
        self.assertIn("code6", cast(dict[str, object], json_object(pool)["reviewers"]))
        result, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(result, workers.ReviewerMigrated("code6", "builders-unit", "repository-review2", mesh_fixture.THREAD_ID))
        self.assertEqual(json_object(target_inbox)["messages"], expected)
        self.assertEqual(json_object(old_inbox)["messages"], [])
        self.assertEqual(json_object(pool)["reviewers"], {})
        before = old_inbox.read_bytes(), target_inbox.read_bytes(), pool.read_bytes()
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), ())
        self.assertEqual((old_inbox.read_bytes(), target_inbox.read_bytes(), pool.read_bytes()), before)

    def test_destination_ledger_stays_locked_from_source_fence_through_registration(self) -> None:
        pool, source = self.leased_pool_reviewer()
        write_roster = workers._write_roster  # pyright: ignore[reportPrivateUsage]
        lock_path = workers.ledger_for_run(self.session_dir).path.with_suffix(".lock")
        checked: list[bool] = []

        def inspect_lock(handle: TextIO, data: dict[str, object]) -> None:
            if Path(cast(str, handle.name)) in (source / codex_mesh.ROSTER_FILE,
                                               self.session_dir / codex_mesh.ROSTER_FILE):
                with lock_path.open() as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                checked.append(True)
            write_roster(handle, data)

        with patch.object(workers, "_write_roster", new=inspect_lock):
            result, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertIsInstance(result, workers.ReviewerMigrated)
        self.assertEqual(len(checked), 2)
        self.assertEqual(json_object(pool)["reviewers"], {})

    def test_dry_run_checks_live_turns_and_detached_work_without_creating_files(self) -> None:
        pool, source = self.leased_pool_reviewer()
        paths = {path: path.read_bytes() for path in self.session_dir.parent.rglob("*") if path.is_file()}
        original = self.server.respond

        @mesh_fixture.bounded_fixture
        def active_thread(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [{"id": request["id"], "result": {"data": [mesh_fixture.THREAD_ID]}}]
            if request.get("method") == "thread/read":
                return [{"id": request["id"], "result": {"thread": {"id": mesh_fixture.THREAD_ID,
                    "status": {"type": "active"}, "turns": [{"id": "live", "status": "inProgress"}]}}}]
            return original(request)

        self.server.respond = active_thread
        dry, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir})
        self.assertIsInstance(dry, workers.ReviewerDeferred)
        self.assertEqual({path: path.read_bytes() for path in self.session_dir.parent.rglob("*") if path.is_file()}, paths)
        applied, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(dry, applied)
        self.server.respond = original
        runs = source / codex_mesh.DETACHED_RUNS_DIR
        runs.mkdir()
        _ = (runs / "review-command.json").write_text(json.dumps({
            "worker": "code6", "status": "running", "pid": os.getpid()}))
        dry, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir})
        self.assertIsInstance(dry, workers.ReviewerDeferred)
        self.assertIn("detached", dry.reason if isinstance(dry, workers.ReviewerDeferred) else "")
        self.assertIn("code6", cast(dict[str, object], json_object(pool)["reviewers"]))

    def test_transfer_cli_reads_unit_identity_from_the_existing_plan(self) -> None:
        pool, source = self.leased_pool_reviewer()
        state = json_object(self.session_dir / workers.STATE)
        _ = state.pop("unit")
        _ = state.pop("production")
        plan = self.session_dir.parent / "repository" / "unit.md"
        _ = plan.write_text('> **Production: production** — unit `builders-unit`; fixture\n')
        state["project_plan_doc"] = "unit.md"
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        paths = {path: path.read_bytes() for path in self.session_dir.parent.rglob("*") if path.is_file()}
        with patch.object(sys, "argv", [str(SCRIPT), "transfer-reviewers", "--production", "production",
                                        "--session-dir", str(self.session_dir)]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workers.main(), 0)
        self.assertIn("preview: would migrate: builders-unit: code6 -> repository-review", output.getvalue())
        self.assertEqual({path: path.read_bytes() for path in self.session_dir.parent.rglob("*") if path.is_file()}, paths)
        self.assertTrue(pool.exists() and source.exists())

    def test_reviewer_names_use_the_launcher_rule_for_nested_unusual_worktrees(self) -> None:
        working = self.session_dir.parent / ("odd worktree !" + "x" * 50)
        working.mkdir()
        _ = subprocess.run(["git", "init", "-q", str(working)], check=True, capture_output=True)
        nested = working / "nested"
        nested.mkdir()
        state = json_object(self.session_dir / workers.STATE)
        state["working_dir"] = str(nested)
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        expected = subprocess.run(["bash", str(SCRIPT.with_name("worker_name.sh")), str(nested), "review", "review"],
                                  check=True, capture_output=True, text=True).stdout.strip()
        self.configure_new_worker()
        registry = self.session_dir.parent / "registry.conf"
        with registry.open("a") as output:
            _ = output.write("review=gpt-kept-sol:xhigh\n")
        self.assertEqual(self.resolve(self.session_dir, "review"), expected)
        self.assertTrue(expected.startswith("odd-worktree--"), expected)
        self.assertEqual(len(expected.removesuffix("-review")), 40)

    def test_launcher_worktree_names_the_same_reviewer_without_run_state_on_rerun(self) -> None:
        (self.session_dir / workers.STATE).unlink()
        (self.session_dir / "working_dir").unlink()
        store = workers.ledger_for_run(self.session_dir)
        working = str(self.session_dir.parent / "repository")
        self.configure_new_worker()
        with (self.session_dir.parent / "registry.conf").open("a") as output:
            _ = output.write("review=gpt-kept-sol:xhigh\n")

        first = self.resolve(self.session_dir, "review", working_dir=working)
        self.assertEqual(first, "repository-review")
        self.assertEqual(workers.ledger_for_run(self.session_dir).path, store.path)
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        thread = cast(dict[str, object], roster[first])["thread_id"]
        self.complete_assignment(first, role="review")

        self.assertEqual(self.resolve(self.session_dir, "review", working_dir=working), first)
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        self.assertEqual(set(roster), {first})
        self.assertEqual(cast(dict[str, object], roster[first])["thread_id"], thread)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertFalse((self.session_dir / workers.STATE).exists())
        self.assertFalse((self.session_dir / "working_dir").exists())

    def test_reviewer_without_state_or_launcher_worktree_is_unavailable(self) -> None:
        (self.session_dir / workers.STATE).unlink()
        (self.session_dir / "working_dir").unlink()
        with self.assertRaisesRegex(workers.WorkerUnavailable, "^invalid worker state: missing working_dir$"):
            _ = workers.resolve(self.session_dir, "review")
        self.assertEqual(self.methods("thread/start"), [])

    def test_reviewer_state_worktree_is_authoritative_over_the_launcher(self) -> None:
        self.configure_new_worker()
        with (self.session_dir.parent / "registry.conf").open("a") as output:
            _ = output.write("review=gpt-kept-sol:xhigh\n")
        self.assertEqual(self.resolve(self.session_dir, "review", working_dir=str(self.session_dir)),
                         "repository-review")

    def test_launcher_worktree_names_an_existing_unit_reviewer_during_lease_release(self) -> None:
        pool, _source = self.own_reviewer_with_six_pool_leases()
        state = json_object(self.session_dir / workers.STATE)
        working = str(state.pop("working_dir"))
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        self.assertEqual(self.resolve(self.session_dir, "review", working_dir=working), "repository-review")
        with review_pool.ProductionPool(pool.parent).locked(write=False) as saved:
            self.assertEqual(len(saved.reviewers), 6)
            self.assertTrue(all(isinstance(reviewer.lease, review_pool.Unleased) for reviewer in saved.reviewers.values()))
        self.assertEqual(self.methods("thread/start"), [])

    def test_transfer_conflicts_keep_closed_causes_and_plain_cli_messages(self) -> None:
        pool, source = self.leased_pool_reviewer()
        missing, = workers.transfer_reviewers("production", {})
        self.assertIsInstance(missing, workers.ReviewerConflict)
        if isinstance(missing, workers.ReviewerConflict):
            self.assertIs(missing.cause, workers.ReviewerConflictCause.NO_DESTINATION)
            self.assertEqual(missing.reason, "no destination run is named")
        state = json_object(self.session_dir / workers.STATE)
        state["unit"] = "another-unit"
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        wrong, = workers.transfer_reviewers("production", {"builders-unit": self.session_dir})
        self.assertIsInstance(wrong, workers.ReviewerConflict)
        if isinstance(wrong, workers.ReviewerConflict):
            self.assertIs(wrong.cause, workers.ReviewerConflictCause.WRONG_UNIT)
        self.assertTrue(pool.exists() and source.exists())

    def test_a_unit_with_no_leased_code_reviewer_has_no_transfer_entry(self) -> None:
        pool, source = self.leased_pool_reviewer()
        data = json_object(pool)
        reviewers = cast(dict[str, object], data["reviewers"])
        leased = cast(dict[str, object], reviewers["code6"])
        reviewers["ui2"] = {**leased, "type": "ui"}
        leased["lease"] = None
        _ = pool.write_text(json.dumps(data), encoding="utf-8")
        before = pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), ())
        self.assertEqual((pool.read_bytes(), (source / codex_mesh.ROSTER_FILE).read_bytes()), before)
        self.assertFalse(workers.ledger_for_run(self.session_dir).path.exists())

    def several_leased_pool_reviewers(self, count: int) -> tuple[Path, Path]:
        pool, source = self.leased_pool_reviewer()
        data = json_object(pool)
        reviewers = cast(dict[str, object], data["reviewers"])
        newest = cast(dict[str, object], reviewers["code6"])
        newest["last_allocated"] = 300.0
        records = json_object(source / codex_mesh.ROSTER_FILE)
        for index in range(7, 6 + count):
            name = f"code{index}"
            reviewers[name] = {**newest, "thread_id": f"older-thread-{index}",
                               "last_allocated": float(index),
                               "lease": {"unit": "builders" if index == 8 else "builders-unit", "ordinal": index}}
            records[name] = {**cast(dict[str, object], records["code6"]), "thread_id": f"older-thread-{index}"}
        _ = pool.write_text(json.dumps(data), encoding="utf-8")
        _ = (source / codex_mesh.ROSTER_FILE).write_text(json.dumps(records), encoding="utf-8")
        return pool, source

    def assert_one_reviewer_adopted(self, pool: Path, count: int) -> None:
        remaining = review_pool.decode_pool(cast(review_pool.PoolJson, cast(object, json_object(pool))))
        self.assertEqual(set(remaining.reviewers), {f"code{index}" for index in range(7, 6 + count)})
        for reviewer in remaining.reviewers.values():
            self.assertIsInstance(reviewer.lease, review_pool.Unleased)
        destination = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        self.assertEqual(set(destination), {"repository-review"})
        record = cast(dict[str, object], destination["repository-review"])
        self.assertEqual((record["slot"], record["role"], record["thread_id"]),
                         ("review", "review", mesh_fixture.THREAD_ID))
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertEqual(set(ledger.workers), {"repository-review"})
            self.assertEqual(ledger.workers["repository-review"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
            self.assertEqual(ledger.workers["repository-review"].access_policy.get("kind"), "ReadOnlyReviewer")
            self.assertTrue(ledger.reviewers_adopted)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("thread/fork"), [])

    def test_two_leased_reviewers_adopt_only_the_most_recently_allocated_thread(self) -> None:
        pool, _ = self.several_leased_pool_reviewers(2)
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), (
            workers.ReviewerMigrated("code6", "builders-unit", "repository-review", mesh_fixture.THREAD_ID),
            workers.ReviewerReleased("code7", "builders-unit"),
        ))
        self.assert_one_reviewer_adopted(pool, 2)
        before = {path: path.read_bytes() for path in self.session_dir.parent.rglob("*") if path.is_file()}
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), ())
        self.assertEqual({path: path.read_bytes() for path in self.session_dir.parent.rglob("*") if path.is_file()}, before)

    def test_first_review_with_three_leases_adopts_the_newest_without_a_conflict(self) -> None:
        pool, _ = self.several_leased_pool_reviewers(3)
        self.assertEqual(self.resolve(self.session_dir, "review"), "repository-review")
        self.assert_one_reviewer_adopted(pool, 3)

    def test_multiple_lease_dry_run_reports_the_same_choice_and_releases_without_writes(self) -> None:
        pool, _ = self.several_leased_pool_reviewers(3)
        before = {path: path.read_bytes() if path.is_file() else None for path in self.session_dir.parent.rglob("*")}
        with patch.object(sys, "argv", [str(SCRIPT), "transfer-reviewers", "--production", "production",
                                        "--session-dir", str(self.session_dir)]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workers.main(), 0)
        self.assertIn("preview: would migrate: builders-unit: code6 -> repository-review", output.getvalue())
        self.assertIn("preview: would release: builders-unit: code7: returned to pool", output.getvalue())
        self.assertIn("preview: would release: builders: code8: returned to pool", output.getvalue())
        self.assertEqual({path: path.read_bytes() if path.is_file() else None
                          for path in self.session_dir.parent.rglob("*")}, before)
        with patch.object(sys, "argv", [str(SCRIPT), "transfer-reviewers", "--production", "production",
                                        "--session-dir", str(self.session_dir), "--apply"]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workers.main(), 0)
        self.assertIn("migrated: builders-unit: code6 -> repository-review", output.getvalue())
        self.assertIn("released: builders-unit: code7: returned to pool", output.getvalue())
        self.assertIn("released: builders: code8: returned to pool", output.getvalue())
        self.assert_one_reviewer_adopted(pool, 3)

    def test_multiple_leases_recover_the_saved_choice_after_a_crash(self) -> None:
        pool, _ = self.several_leased_pool_reviewers(3)
        atomic = workers._atomic  # pyright: ignore[reportPrivateUsage]

        def interrupted(path: Path, data: Mapping[str, object]) -> None:
            atomic(path, data)
            if "worker-transfers" in path.parts:
                raise AdoptionWriteInterrupted()

        with patch.object(workers, "_atomic", new=interrupted), self.assertRaises(AdoptionWriteInterrupted):
            _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        data = json_object(pool)
        cast(dict[str, object], cast(dict[str, object], data["reviewers"])["code7"])["last_allocated"] = 400.0
        _ = pool.write_text(json.dumps(data))
        results = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(results[0], workers.ReviewerMigrated("code6", "builders-unit", "repository-review", mesh_fixture.THREAD_ID))
        self.assertEqual({result.reviewer for result in results if isinstance(result, workers.ReviewerReleased)}, {"code7", "code8"})
        self.assert_one_reviewer_adopted(pool, 3)
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), ())

    def test_a_busy_surplus_lease_is_released_on_retry_without_another_adoption(self) -> None:
        pool, _ = self.several_leased_pool_reviewers(2)
        with review_pool.ProductionPool(pool.parent).locked() as state:
            state.reviewers["code7"].ownership = review_pool.ClaimedReviewer(os.getpid())
        results = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertIsInstance(results[0], workers.ReviewerMigrated)
        self.assertIsInstance(results[1], workers.ReviewerDeferred)
        with review_pool.ProductionPool(pool.parent).locked() as state:
            state.reviewers["code7"].ownership = review_pool.IdleReviewer()
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False),
                         (workers.ReviewerReleased("code7", "builders-unit"),))
        self.assert_one_reviewer_adopted(pool, 2)

    def own_reviewer_with_six_pool_leases(self) -> tuple[Path, Path]:
        pool, source = self.leased_pool_reviewer()
        data = json_object(pool)
        template = cast(dict[str, object], cast(dict[str, object], data["reviewers"])["code6"])
        record = cast(dict[str, object], json_object(source / codex_mesh.ROSTER_FILE)["code6"])
        names = ("code3", "code4", "code8", "code9", "code10", "code11")
        data["reviewers"] = {name: {**template, "thread_id": f"pool-{name}", "last_allocated": index}
                             for index, name in enumerate(names)}
        _ = pool.write_text(json.dumps(data))
        _ = (source / codex_mesh.ROSTER_FILE).write_text(json.dumps({
            name: {**record, "thread_id": f"pool-{name}"} for name in names}))
        self.open_worker("repository-review", "review")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["repository-review"]).update({"status": "paused", "paused_from": "done"})
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        # Match resources-unit: the reviewer predates the unit ledger.
        with workers.ledger_for_run(self.session_dir).locked():
            pass
        return pool, source

    def test_own_roster_reviewer_releases_six_leases_and_registers_without_another_thread(self) -> None:
        pool, source = self.own_reviewer_with_six_pool_leases()
        source_before = (source / codex_mesh.ROSTER_FILE).read_bytes()
        self.assertEqual(self.resolve(self.session_dir, "review"), "repository-review")
        with review_pool.ProductionPool(pool.parent).locked(write=False) as state:
            self.assertEqual(set(state.reviewers), {"code3", "code4", "code8", "code9", "code10", "code11"})
            self.assertTrue(all(isinstance(reviewer.lease, review_pool.Unleased) for reviewer in state.reviewers.values()))
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        self.assertEqual(set(roster), {"repository-review"})
        self.assertEqual(cast(dict[str, object], roster["repository-review"])["thread_id"], mesh_fixture.THREAD_ID)
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertTrue(ledger.reviewers_adopted)
            self.assertEqual(set(ledger.workers), {"repository-review"})
            self.assertEqual(ledger.workers["repository-review"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
        self.assertEqual((source / codex_mesh.ROSTER_FILE).read_bytes(), source_before)
        before = {path: path.read_bytes() if path.is_file() else None for path in self.session_dir.parent.rglob("*")}
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), ())
        self.assertEqual({path: path.read_bytes() if path.is_file() else None
                          for path in self.session_dir.parent.rglob("*")}, before)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(self.methods("thread/fork"), [])

    def test_own_reviewer_dry_run_names_six_releases_without_writing(self) -> None:
        pool, _ = self.own_reviewer_with_six_pool_leases()
        # Also cover a destination with no ledger file yet.
        workers.ledger_for_run(self.session_dir).path.unlink()
        before = {path: path.read_bytes() if path.is_file() else None for path in self.session_dir.parent.rglob("*")}
        with patch.object(sys, "argv", [str(SCRIPT), "transfer-reviewers", "--production", "production",
                                        "--session-dir", str(self.session_dir)]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(workers.main(), 0)
        self.assertEqual(output.getvalue().count("preview: would release:"), 6)
        for name in ("code3", "code4", "code8", "code9", "code10", "code11"):
            self.assertIn(f"would release: builders-unit: {name}: returned to pool", output.getvalue())
        self.assertNotIn("would migrate", output.getvalue())
        self.assertNotIn("conflict:", output.getvalue())
        self.assertEqual({path: path.read_bytes() if path.is_file() else None
                          for path in self.session_dir.parent.rglob("*")}, before)
        results = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(isinstance(result, workers.ReviewerReleased) for result in results))
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertTrue(ledger.reviewers_adopted)
        self.assertTrue(all(cast(dict[str, object], reviewer)["lease"] is None
                            for reviewer in cast(dict[str, object], json_object(pool)["reviewers"]).values()))

    def test_own_reviewer_defers_busy_pool_lease_then_finishes_the_receipt(self) -> None:
        pool, _ = self.own_reviewer_with_six_pool_leases()
        with review_pool.ProductionPool(pool.parent).locked() as state:
            state.reviewers["code11"].ownership = review_pool.ClaimedReviewer(os.getpid())
        results = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(sum(isinstance(result, workers.ReviewerReleased) for result in results), 5)
        self.assertEqual(sum(isinstance(result, workers.ReviewerDeferred) for result in results), 1)
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertFalse(ledger.reviewers_adopted)
        with review_pool.ProductionPool(pool.parent).locked() as state:
            state.reviewers["code11"].ownership = review_pool.IdleReviewer()
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False),
                         (workers.ReviewerReleased("code11", "builders-unit"),))
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertTrue(ledger.reviewers_adopted)

    def test_transfer_preserves_queued_messages_and_account_move_identity(self) -> None:
        pool, source = self.leased_pool_reviewer()
        inbox = source / "code6.pending.json"
        messages = ["finish the existing review", "keep the reply address"]
        _ = inbox.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                                        "messages": messages, "accepting_messages": True}), encoding="utf-8")
        marker = {"thread_id": mesh_fixture.THREAD_ID, "turn_id": "original-turn",
                  "label": "next-account", "move_id": "original-account-move"}
        _ = (source / "code6.account-move.json").write_text(json.dumps(marker), encoding="utf-8")
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False),
                         (workers.ReviewerMigrated("code6", "builders-unit", "repository-review", mesh_fixture.THREAD_ID),))
        target_inbox = self.session_dir / "repository-review.pending.json"
        queued = json_object(target_inbox)
        self.assertEqual((queued["thread_id"], queued["messages"], queued["accepting_messages"]),
                         (mesh_fixture.THREAD_ID, messages, True))
        self.assertEqual(json_object(inbox)["messages"], [])
        self.assertFalse(json_object(inbox)["accepting_messages"])
        self.assertEqual(json_object(self.session_dir / "repository-review.account-move.json"), marker)
        before = target_inbox.read_bytes(), pool.read_bytes()
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False), ())
        self.assertEqual((target_inbox.read_bytes(), pool.read_bytes()), before)

    def assert_pool_transfer_recovers_after(self, stage: Literal["intent", "source", "destination", "ledger"]) -> None:
        pool, source = self.leased_pool_reviewer()
        atomic = workers._atomic  # pyright: ignore[reportPrivateUsage]
        write_roster = workers._write_roster  # pyright: ignore[reportPrivateUsage]
        ledger_path = workers.ledger_for_run(self.session_dir).path
        observed: list[str] = []
        _ = (source / "code6.pending.json").write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
            "messages": ["keep this review request"], "accepting_messages": True}), encoding="utf-8")

        def interrupt(written: str) -> None:
            if written == stage:
                observed.append(written)
                raise AdoptionWriteInterrupted()

        def persist_intent(path: Path, data: Mapping[str, object]) -> None:
            atomic(path, data)
            if "worker-transfers" in path.parts:
                interrupt("intent")
            elif path == ledger_path:
                interrupt("ledger")

        def roster_written(handle: TextIO, roster: dict[str, object]) -> None:
            write_roster(handle, roster)
            path = Path(cast(str, handle.name))
            if path == source / codex_mesh.ROSTER_FILE:
                interrupt("source")
            elif path == self.session_dir / codex_mesh.ROSTER_FILE:
                interrupt("destination")

        with (patch.object(workers, "_atomic", new=persist_intent),
              patch.object(workers, "_write_roster", new=roster_written)):
            with self.assertRaises(AdoptionWriteInterrupted):
                _ = workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False)
        self.assertEqual(observed, [stage])
        self.assertIn("code6", cast(dict[str, object], json_object(pool)["reviewers"]))
        if stage == "intent":
            self.assertFalse(ledger_path.exists())
        self.assertEqual(workers.transfer_reviewers("production", {"builders-unit": self.session_dir}, dry_run=False),
                         (workers.ReviewerMigrated("code6", "builders-unit", "repository-review", mesh_fixture.THREAD_ID),))
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertEqual(set(ledger.workers), {"repository-review"})
            self.assertEqual(ledger.workers["repository-review"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
        self.assertNotIn("code6", cast(dict[str, object], json_object(pool)["reviewers"]))
        self.assertEqual(cast(dict[str, object], json_object(source / codex_mesh.ROSTER_FILE)["code6"])["status"],
                         "ended")
        self.assertEqual(json_object(self.session_dir / "repository-review.pending.json")["messages"],
                         ["keep this review request"])
        self.assertEqual(json_object(source / "code6.pending.json")["messages"], [])
        self.assertEqual(self.methods("thread/start"), [])

    def test_interrupted_pool_transfer_resumes_the_saved_thread_without_duplicate_workers(self) -> None:
        self.assert_pool_transfer_recovers_after("intent")

    def test_pool_transfer_recovers_after_fencing_the_source(self) -> None:
        self.assert_pool_transfer_recovers_after("source")

    def test_pool_transfer_recovers_after_writing_the_destination(self) -> None:
        self.assert_pool_transfer_recovers_after("destination")

    def test_pool_transfer_recovers_after_writing_the_unit_ledger(self) -> None:
        self.assert_pool_transfer_recovers_after("ledger")

    def test_pause_keeps_backend_queued_work_and_reservation_until_retirement(self) -> None:
        store = self.lifecycle_worker()
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        record = cast(dict[str, object], roster["w7"])
        record.update({"continuation_owed": True, "allocation_pending": True,
                       "allocation_id": "keep-this-reservation", "allocation_owner_pid": os.getpid(),
                       "allocation_expires_at": 4102444800})
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        inbox = self.session_dir / "w7.pending.json"
        _ = inbox.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                                        "messages": ["keep queued work"], "accepting_messages": True}))
        before = inbox.read_bytes()
        workers.pause_unit("before", "builders")
        with store.locked() as ledger:
            self.assertIsInstance(ledger.workers["w7"].ownership, workers.Paused)
            self.assertEqual(ledger.workers["w7"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
        self.assertEqual(inbox.read_bytes(), before)
        self.assertEqual(json_object(self.session_dir / codex_mesh.ROSTER_FILE), roster)

    def test_retirement_names_cancelled_work_and_quarantine_then_removes_the_ledger(self) -> None:
        store = self.lifecycle_worker()
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["w7"]).update({"continuation_owed": True,
            "allocation_pending": True, "allocation_id": "retiring", "allocation_owner_pid": os.getpid(),
            "allocation_expires_at": 4102444800})
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        _ = (self.session_dir / "w7.pending.json").write_text(json.dumps({
            "thread_id": mesh_fixture.THREAD_ID, "messages": ["cancel this task"]}))
        with store.locked() as ledger:
            ledger.quarantined_workers["damaged"] = {"reason": "invalid backend", "record": {}}
            ledger.quarantined_adoptions["handoff"] = {"reason": "revision conflict", "intent": {}}
        output = io.StringIO()
        with bounded_worker_change(), redirect_stdout(output):
            workers.end_unit("before", "builders")
        self.assertFalse(store.path.exists())
        ended = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["w7"])
        self.assertEqual(ended["status"], "ended")
        self.assertFalse(ended["continuation_owed"])
        self.assertFalse(ended.get("allocation_pending"))
        self.assertNotIn("allocation_id", ended)
        self.assertNotIn("cancel this task", (self.session_dir / "w7.pending.json").read_text())
        for evidence in ("cancel this task", "continuation", "reservation", "damaged", "invalid backend",
                         "handoff", "revision conflict", str(store.path), "w7"):
            self.assertIn(evidence, output.getvalue())

    def test_transfer_and_rename_preserve_worker_identity_and_rebind_all_runs(self) -> None:
        original = self.lifecycle_worker()
        side = self.next_run("another-unit-run")
        state = json_object(side / workers.STATE)
        state.update({"production": "before", "unit": "builders"})
        _ = (side / workers.STATE).write_text(json.dumps(state))
        _ = (side / codex_mesh.ROSTER_FILE).write_bytes((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes())
        preserved = {"allocation_pending": True, "allocation_id": "carry-reservation",
                     "allocation_owner_pid": os.getpid(), "allocation_expires_at": 4102444800,
                     "continuation_owed": True}
        for run in (self.session_dir, side):
            roster = json_object(run / codex_mesh.ROSTER_FILE)
            cast(dict[str, object], roster["w7"]).update(preserved)
            _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        with original.locked() as ledger:
            before = ledger.workers["w7"]
            ledger.quarantined_workers["broken"] = {"reason": "keep diagnostic", "record": {}}
        with bounded_worker_change():
            workers.move_unit("before", "after", "builders", runs=(self.session_dir, side))
            workers.move_unit("before", "after", "builders", runs=(self.session_dir, side))
            workers.rename_unit("after", "builders", "renamed", runs=(self.session_dir, side))
        target = workers.ledger_path("after", "renamed")
        self.assertFalse(original.path.exists())
        self.assertFalse(workers.ledger_path("after", "builders").exists())
        with workers.UnitWorkerLedger(target).locked() as ledger:
            self.assertEqual(ledger.workers["w7"], before)
            self.assertEqual(ledger.quarantined_workers["broken"]["reason"], "keep diagnostic")
        for run in (self.session_dir, side):
            self.assertEqual(workers.ledger_for_run(run).path, target)
            self.assertEqual(json_object(run / workers.STATE)["production"], "after")
            self.assertEqual(json_object(run / workers.STATE)["unit"], "renamed")
            record = cast(dict[str, object], json_object(run / codex_mesh.ROSTER_FILE)["w7"])
            self.assertEqual(record["unit_ledger"], str(target))
            self.assertEqual({key: record[key] for key in preserved}, preserved)

    def test_a_move_refuses_another_owners_destination_without_changing_either_ledger(self) -> None:
        source = self.lifecycle_worker()
        target = workers.UnitWorkerLedger(workers.ledger_path("after", "builders"))
        with source.locked() as ledger, target.locked() as destination:
            destination.workers["other-owner"] = ledger.workers["w7"]
        snapshots = (source.path.read_bytes(), target.path.read_bytes())
        with bounded_worker_change(), self.assertRaises(workers.WorkerUnavailable):
            workers.move_unit("before", "after", "builders", runs=(self.session_dir,))
        self.assertEqual((source.path.read_bytes(), target.path.read_bytes()), snapshots)

    def test_a_move_recovers_after_each_durable_write_without_a_second_owner(self) -> None:
        for cut in range(1, 7):
            with self.subTest(write=cut):
                store = self.lifecycle_worker()
                target = workers.ledger_path("after", "builders")
                target.unlink(missing_ok=True)
                atomic = workers._atomic  # pyright: ignore[reportPrivateUsage]
                write_roster = workers._write_roster  # pyright: ignore[reportPrivateUsage]
                writes = 0
                set_unit = progress_history.set_unit

                def after_write() -> None:
                    nonlocal writes
                    writes += 1
                    if writes == cut:
                        raise AdoptionWriteInterrupted()

                def persist(path: Path, data: Mapping[str, object]) -> None:
                    atomic(path, data)
                    after_write()

                def roster_write(handle: TextIO, data: dict[str, object]) -> None:
                    write_roster(handle, data)
                    after_write()

                def unit_written(run: Path, production: str, unit: str) -> None:
                    set_unit(run, production, unit)
                    after_write()

                with (bounded_worker_change(), patch.object(workers, "_atomic", new=persist),
                      patch.object(workers, "_write_roster", new=roster_write),
                      patch.object(progress_history, "set_unit", new=unit_written)):
                    with self.assertRaises(AdoptionWriteInterrupted):
                        workers.move_unit("before", "after", "builders", runs=(self.session_dir,))
                with bounded_worker_change():
                    workers.move_unit("before", "after", "builders", runs=(self.session_dir,))
                self.assertFalse(store.path.exists())
                self.assertEqual(workers.ledger_for_run(self.session_dir).path, target)
                with workers.UnitWorkerLedger(target).locked() as ledger:
                    self.assertEqual(set(ledger.workers), {"w7"})
                    self.assertEqual(ledger.workers["w7"].backend,
                                     workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
                target.unlink()

    def test_retirement_terminates_a_supervisor_retained_in_an_adopted_giving_run(self) -> None:
        _ = self.lifecycle_worker()
        runs = self.session_dir / "mesh_runs"
        runs.mkdir()
        record = runs / "retiring-command.json"
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                                  "mesh_runs.py", "supervise", str(record)], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            _ = record.write_text(json.dumps({"worker": "w7", "status": "running", "pid": child.pid,
                                             "cwd": str(self.session_dir)}))
            workers.pause_unit("before", "builders")
            destination = self.next_run("retire-adopted-command")
            state = json_object(destination / workers.STATE)
            state.update({"production": "before", "unit": "builders"})
            _ = (destination / workers.STATE).write_text(json.dumps(state))
            with bounded_worker_change():
                self.assertEqual(workers.adopt(self.session_dir, destination, "w7"), "w7")
            self.assertIn(str(self.session_dir), codex_mesh.worker_run_history(str(destination), "w7"))
            with bounded_worker_change(), redirect_stdout(io.StringIO()) as output:
                workers.end_unit("before", "builders")
            _ = child.communicate(timeout=4)
            self.assertEqual(child.returncode, -signal.SIGTERM)
            self.assertIn("retiring-command", output.getvalue())
            self.assertIn(str(child.pid), output.getvalue())
            self.assertTrue(record.exists(), "retirement must retain detached-run records")
        finally:
            if child.poll() is None:
                child.kill()
                _ = child.communicate(timeout=4)

    def test_retirement_never_trusts_or_signals_a_reused_supervisor_pid(self) -> None:
        _ = self.lifecycle_worker()
        records = self.session_dir / "mesh_runs"
        records.mkdir()
        record = records / "abandoned.json"
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as unrelated:
            _ = record.write_text(json.dumps({"worker": "w7", "status": "running", "pid": unrelated.pid,
                                             "cwd": str(self.session_dir)}))
            workers.pause_unit("before", "builders")
            self.assertEqual(workers.live_detached_runs(self.session_dir), ())
            self.assertEqual(workers.protected_working_directories(self.session_dir), ())
            self.assertEqual(workers.retained_detached_runs(self.session_dir), ())
            real_kill = os.kill
            def refuse_foreign_signal(pid: int, number: int) -> None:
                if pid == unrelated.pid and number:
                    raise AssertionError("retirement signalled a process that is not the recorded supervisor")
                real_kill(pid, number)
            with patch.object(os, "kill", new=refuse_foreign_signal), redirect_stdout(io.StringIO()) as output:
                workers.end_unit("before", "builders")
            self.assertIsNone(unrelated.poll())
            self.assertNotIn("cancelled detached run abandoned", output.getvalue())
            _ = unrelated.communicate(timeout=4)

    def test_a_supervisor_started_through_a_linked_run_protects_its_resolved_checkout(self) -> None:
        _ = self.lifecycle_worker()
        linked = self.session_dir.parent / "linked-run"
        linked.symlink_to(self.session_dir, target_is_directory=True)
        records = self.session_dir / "mesh_runs"
        records.mkdir()
        record = records / "linked-command.json"
        checkout = self.session_dir / "phases" / "5" / "gate"
        checkout.mkdir(parents=True)
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
            "mesh_runs.py", "supervise", str(linked / "mesh_runs" / record.name)], stdin=subprocess.PIPE)
        self.addCleanup(child.communicate, timeout=4)
        _ = record.write_text(json.dumps({"worker": "w7", "status": "running", "pid": child.pid,
                                         "cwd": str(checkout)}))
        workers.pause_unit("before", "builders")
        self.assertEqual([path for path, _ in workers.live_detached_runs(self.session_dir.resolve())], [record])
        self.assertEqual(workers.protected_working_directories(self.session_dir.resolve()), (checkout.resolve(),))

    def test_move_finishes_a_pending_adoption_before_rebinding_both_runs(self) -> None:
        source_store = self.lifecycle_worker()
        destination = self.next_run("adoption-before-migration")
        state = json_object(destination / workers.STATE)
        state.update({"production": "before", "unit": "builders"})
        _ = (destination / workers.STATE).write_text(json.dumps(state))

        def interrupted(stage: str) -> None:
            if stage == "source":
                raise AdoptionWriteInterrupted()

        with bounded_worker_change(), observe_adoption_writes(self.session_dir, destination, interrupted):
            with self.assertRaises(AdoptionWriteInterrupted):
                _ = workers.adopt(self.session_dir, destination, "w7")
        with bounded_worker_change():
            workers.move_unit("before", "after", "builders", runs=(self.session_dir, destination))
        target = workers.ledger_path("after", "builders")
        self.assertFalse(source_store.path.exists())
        with workers.UnitWorkerLedger(target).locked() as ledger:
            self.assertEqual(ledger.adoptions, {})
            self.assertEqual(ledger.workers["w7"].ownership, workers.Adopted(str(destination)))
        for run in (self.session_dir, destination):
            self.assertEqual(workers.ledger_for_run(run).path, target)
            self.assertEqual(cast(dict[str, object], json_object(run / codex_mesh.ROSTER_FILE)["w7"])["unit_ledger"], str(target))

    def test_paused_inbox_handoff_recovers_after_either_durable_queue_write(self) -> None:
        for interrupted_write in (1, 2):
            with self.subTest(inbox_write=interrupted_write):
                _ = self.lifecycle_worker()
                workers.pause_unit("before", "builders")
                destination = self.next_run(f"inbox-write-{interrupted_write}")
                state = json_object(destination / workers.STATE)
                state.update({"production": "before", "unit": "builders"})
                _ = (destination / workers.STATE).write_text(json.dumps(state))
                source_inbox = self.session_dir / "w7.pending.json"
                target_inbox = destination / "w7.pending.json"
                _ = source_inbox.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                    "messages": ["keep this task once"], "accepting_messages": True}))
                persist = workers._write_inbox  # pyright: ignore[reportPrivateUsage]
                writes = 0

                def crash_after_inbox(handle: TextIO, record: Mapping[str, object]) -> None:
                    nonlocal writes
                    persist(handle, record)
                    writes += 1
                    if writes == interrupted_write:
                        raise AdoptionWriteInterrupted()

                with bounded_worker_change(), patch.object(workers, "_write_inbox", new=crash_after_inbox):
                    with self.assertRaises(AdoptionWriteInterrupted):
                        _ = workers.adopt(self.session_dir, destination, "w7")
                with bounded_worker_change():
                    held = codex_mesh.hold_message(str(self.session_dir), "w7", "arrived during recovery",
                                                  without_launcher=True)
                self.assertIsInstance(held, codex_mesh.HeldForNextDispatch)
                with bounded_worker_change():
                    workers.recover_adoptions(destination)
                self.assertEqual(json_object(target_inbox)["messages"], ["keep this task once", "arrived during recovery"])
                self.assertEqual(json_object(source_inbox)["messages"], [])
                self.assertEqual(cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)["w7"])["thread_id"], mesh_fixture.THREAD_ID)

    def test_old_adoption_journals_preserve_their_declared_queue_policy(self) -> None:
        for transfer_queue in (False, True):
            with self.subTest(transfer_queue=transfer_queue):
                store = self.lifecycle_worker()
                inbox = self.session_dir / "w7.pending.json"
                _ = inbox.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID, "messages": []}))
                destination = self.next_run(f"legacy-queue-{transfer_queue}")
                state = json_object(destination / workers.STATE)
                state.update({"production": "before", "unit": "builders"})
                _ = (destination / workers.STATE).write_text(json.dumps(state))
                def interrupted(stage: str) -> None:
                    if stage == "intent":
                        raise AdoptionWriteInterrupted()
                with bounded_worker_change(), observe_adoption_writes(self.session_dir, destination, interrupted):
                    with self.assertRaises(AdoptionWriteInterrupted):
                        _ = workers.adopt(self.session_dir, destination, "w7")
                data = json_object(store.path)
                intents = cast(dict[str, object], data["adoptions"])
                for raw in intents.values():
                    intent = cast(dict[str, object], raw)
                    self.assertEqual(intent.pop("queue_policy"), "transfer")
                    intent["carries_work"] = transfer_queue
                _ = store.path.write_text(json.dumps(data))
                _ = inbox.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                                                "messages": ["arrived after intent"]}))
                with bounded_worker_change():
                    if transfer_queue:
                        workers.recover_adoptions(self.session_dir)
                    else:
                        with self.assertRaises(workers.WorkerUnavailable):
                            workers.recover_adoptions(self.session_dir)
                if transfer_queue:
                    self.assertEqual(json_object(destination / "w7.pending.json")["messages"], ["arrived after intent"])
                    self.assertEqual(json_object(inbox)["messages"], [])
                else:
                    self.assertEqual(json_object(inbox)["messages"], ["arrived after intent"])
                    self.assertNotIn("w7", json_object(destination / codex_mesh.ROSTER_FILE))
                    workers.recover_adoptions(self.session_dir)

    def test_explicit_reservation_owner_survives_resolve_and_becomes_reclaimable_when_it_exits(self) -> None:
        _ = self.lifecycle_worker(name="impl")
        self.configure_new_worker()
        owner = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            result = subprocess.run([sys.executable, str(SCRIPT), "resolve", "--session-dir", str(self.session_dir),
                "--role", "impl", "--owner-pid", str(owner.pid)], capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "impl")
            record = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
            self.assertEqual(record["allocation_owner_pid"], owner.pid)
            self.assertTrue(workers.allocation_is_live(record))
            _ = owner.communicate(timeout=4)
            self.assertFalse(workers.allocation_is_live(record))
            self.assertEqual(self.resolve(self.session_dir), "impl")
            self.assertEqual(self.methods("thread/start"), [])
        finally:
            if owner.poll() is None:
                owner.kill()
                _ = owner.communicate(timeout=4)

    def test_resolve_from_a_short_lived_shell_keeps_the_directors_reservation(self) -> None:
        _ = self.lifecycle_worker(name="impl")
        self.configure_new_worker()
        registry = self.session_dir / "director-sessions"
        registry.mkdir()
        from scripts.lib import unit_runs
        from scripts.message import sessions
        director = "fixture-director"
        _ = (self.session_dir / unit_runs.DIRECTOR_FILE).write_text(director)
        with socket.socket(socket.AF_UNIX) as endpoint:
            address = self.session_dir / "director.sock"
            endpoint.bind(str(address))
            _ = (registry / f"{os.getpid()}.json").write_text(json.dumps({
                "pid": os.getpid(), "sessionId": director, "name": "demo · builders · director",
                "messagingSocketPath": str(address), "updatedAt": 1, "tmux": "", "formerNames": [],
                "procStart": sessions.process_start(os.getpid()), "entrypoint": "cli"}))
            self.patches.enter_context(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(registry)}))
            self.assertEqual(workers.director_pid(self.session_dir), os.getpid())
            self.check_short_lived_resolve()

    def check_short_lived_resolve(self) -> None:
        command = shlex.join([sys.executable, str(SCRIPT), "resolve", "--session-dir", str(self.session_dir), "--role", "impl"])
        first = subprocess.run(["bash", "-c", command + "; true"], capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(first.stdout.strip(), "impl", first.stderr)
        record = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
        self.assertEqual(record["allocation_owner_pid"], os.getpid())
        self.assertTrue(workers.allocation_is_live(record))
        self.assertEqual(self.resolve(self.session_dir), "impl2")

    def test_a_swept_run_keeps_its_worker_thread_and_home_in_the_unit_ledger(self) -> None:
        self.open_worker(name="impl")
        self.configure_new_worker()
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        destination = self.next_run("after-run-sweep")
        original_home = os.environ["CODEX_HOME"]
        shutil.rmtree(self.session_dir)

        self.assertEqual(self.resolve(destination), "impl")
        self.assertFalse(self.session_dir.exists(), "carryover recreated the swept run")
        record = cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)["impl"])
        self.assertEqual(record["thread_id"], mesh_fixture.THREAD_ID)
        with workers.ledger_for_run(destination).locked() as ledger:
            self.assertEqual(ledger.workers["impl"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, original_home))
        prompt = destination / "continue.txt"
        _ = prompt.write_text("Continue the unit's existing worker.")
        with bounded_worker_change(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.main([
                "follow", "--session-dir", str(destination), "--to", "impl", "--role", "impl",
                "--message-file", str(prompt), "--summary-file", str(destination / "summary.txt"),
                "--log-file", str(destination / "impl.log"), "--timeout", "5",
            ])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertTrue(self.methods("thread/resume"))
        self.assertTrue(all(cast(dict[str, object], request["params"])["threadId"] == mesh_fixture.THREAD_ID
                            for request in self.methods("thread/resume")))
        self.assertEqual(self.methods("thread/start"), [])

    def test_queued_work_and_owed_continuations_exclude_same_run_allocation(self) -> None:
        self.configure_new_worker()
        for obstacle in ("continuation", "pending"):
            with self.subTest(obstacle=obstacle):
                state = json_object(self.session_dir / workers.STATE)
                state.update({"production": "same-run-fixture", "unit": obstacle})
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.open_worker(name="impl")
                roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
                record = cast(dict[str, object], roster["impl"])
                pending = self.session_dir / "impl.pending.json"
                pending.unlink(missing_ok=True)
                if obstacle == "continuation":
                    record["continuation_owed"] = True
                else:
                    _ = pending.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                        "messages": ["Previously queued work."], "accepting_messages": True}))
                _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
                original = dict(record)

                self.assertEqual(self.resolve(self.session_dir), "impl2")
                after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
                self.assertEqual(after, original)
                if obstacle == "pending":
                    self.assertEqual(json_object(pending)["messages"], ["Previously queued work."])

    def test_two_simultaneous_resolves_in_one_run_reserve_distinct_workers(self) -> None:
        self.open_worker(name="impl")
        self.configure_new_worker()
        gate = threading.Barrier(2, timeout=4)

        def reserve() -> str:
            _ = gate.wait()
            return self.resolve(self.session_dir)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(reserve) for _ in range(2)]
            names = [future.result(timeout=20) for future in futures]
        self.assertEqual(set(names), {"impl", "impl2"})
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        self.assertEqual({cast(dict[str, object], roster[name])["thread_id"] for name in names},
                         {mesh_fixture.THREAD_ID, "second-thread"})
        self.assertTrue(all(cast(dict[str, object], roster[name]).get("allocation_pending") is True
                            for name in names))
        self.assertEqual(len(self.methods("thread/start")), 1)

    def test_a_reservation_from_an_exited_resolver_reuses_its_thread_in_either_run(self) -> None:
        self.configure_new_worker()
        for destination_name in ("same-run", "next-run"):
            with self.subTest(destination=destination_name):
                state = json_object(self.session_dir / workers.STATE)
                state.update({"production": "abandoned-resolver", "unit": destination_name})
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.open_worker(name="impl")
                child = subprocess.Popen([
                    sys.executable, "-c",
                    "from pathlib import Path; import sys; from scripts.direct import workers; " +
                    "print(workers.resolve(Path(sys.argv[1]), 'impl'), flush=True)",
                    str(self.session_dir),
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
                try:
                    output, errors = child.communicate(timeout=15)
                    self.assertEqual(child.returncode, 0, errors.decode())
                    self.assertEqual(output.decode().strip(), "impl")
                finally:
                    if child.poll() is None:
                        child.kill()
                        _ = child.communicate(timeout=4)
                destination = (self.session_dir if destination_name == "same-run"
                               else self.next_run("after-resolver-exit"))
                if destination != self.session_dir:
                    _ = (destination / workers.STATE).write_text(json.dumps({**state, "run_id": destination.name}))
                self.assertEqual(self.resolve(destination), "impl")
                record = cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)["impl"])
                self.assertEqual(record["thread_id"], mesh_fixture.THREAD_ID)
                self.assertEqual(self.methods("thread/start"), [])

    def test_an_expired_or_legacy_reservation_does_not_strand_an_idle_worker(self) -> None:
        self.configure_new_worker()
        for reservation in ("expired", "legacy"):
            with self.subTest(reservation=reservation):
                state = json_object(self.session_dir / workers.STATE)
                state.update({"production": "expired-resolver", "unit": reservation})
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.open_worker(name="impl")
                self.assertEqual(self.resolve(self.session_dir), "impl")
                roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
                record = cast(dict[str, object], roster["impl"])
                if reservation == "expired":
                    record["allocation_expires_at"] = 0
                else:
                    for key in ("allocation_id", "allocation_owner_pid", "allocation_expires_at"):
                        _ = record.pop(key, None)
                _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
                self.assertEqual(self.resolve(self.session_dir), "impl")
                after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
                self.assertEqual(after["thread_id"], mesh_fixture.THREAD_ID)
                self.assertEqual(self.methods("thread/start"), [])

    def test_a_follow_refused_before_claim_releases_its_dispatch_reservation(self) -> None:
        self.configure_new_worker()
        for refusal in ("recovery", "ended"):
            with self.subTest(refusal=refusal):
                state = json_object(self.session_dir / workers.STATE)
                state.update({"production": "refused-resolver", "unit": refusal})
                _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
                self.open_worker(name="impl")
                self.assertEqual(self.resolve(self.session_dir), "impl")
                args = self.start_args()
                args.to = "impl"
                args.message_file = str(self.session_dir / "prompt.txt")
                if refusal == "ended":
                    roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
                    cast(dict[str, object], roster["impl"])["status"] = "ended"
                    _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
                with bounded_worker_change(), redirect_stderr(io.StringIO()) as errors:
                    if refusal == "recovery":
                        with patch.object(workers, "recover_adoptions",
                                          side_effect=workers.WorkerUnavailable("recovery must be retried")):
                            code = codex_mesh.command_follow(args)
                    else:
                        code = codex_mesh.command_follow(args)
                self.assertEqual(code, 2, errors.getvalue())
                roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
                record = cast(dict[str, object], roster["impl"])
                self.assertFalse(record.get("allocation_pending"), "refusal left the dispatch stranded")
                record["status"] = "done"
                _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
                self.assertEqual(self.resolve(self.session_dir), "impl")
                self.assertEqual(self.methods("turn/start"), [])
                self.assertEqual(self.methods("thread/start"), [])

    def test_a_refused_follow_does_not_release_a_newer_dispatch_reservation(self) -> None:
        self.open_worker(name="impl")
        self.assertEqual(self.resolve(self.session_dir), "impl")
        old = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
        recovery = workers.recover_adoptions
        replacement: dict[str, object] = {}

        def replace_dispatch(run: Path) -> None:
            recovery(run)
            roster = json_object(run / codex_mesh.ROSTER_FILE)
            cast(dict[str, object], roster["impl"])["allocation_expires_at"] = 0
            _ = (run / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
            self.assertEqual(workers.resolve(run, "impl"), "impl")
            replacement.update(cast(dict[str, object], json_object(run / codex_mesh.ROSTER_FILE)["impl"]))
            raise workers.WorkerUnavailable("old follow lost its dispatch")

        args = self.start_args()
        args.to = "impl"
        with (bounded_worker_change(), patch.object(workers, "recover_adoptions", new=replace_dispatch),
              redirect_stderr(io.StringIO()) as errors):
            code = codex_mesh.command_follow(args)
        self.assertEqual(code, 2, errors.getvalue())
        after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"])
        self.assertNotEqual(replacement["allocation_id"], old["allocation_id"])
        self.assertEqual(after, replacement, "stale refusal cleared a newer owner's dispatch")
        self.assertTrue(after.get("allocation_pending"))
        self.assertEqual(self.methods("turn/start"), [])

    def test_finished_run_resolution_requires_reactivation_without_allocating_a_thread(self) -> None:
        self.open_worker(name="impl")
        self.configure_new_worker()
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["impl"])["status"] = "ended"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        state = json_object(self.session_dir / workers.STATE)
        state.update({"status": "completed", "finished_at": 1793200000})
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        before = (self.session_dir / codex_mesh.ROSTER_FILE).read_bytes()
        result = subprocess.run([
            sys.executable, str(SCRIPT), "resolve", "--session-dir", str(self.session_dir), "--role", "impl",
            "--owner-pid", str(os.getpid()),
        ], env=os.environ.copy(), capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("start-phase", result.stderr)
        self.assertEqual((self.session_dir / codex_mesh.ROSTER_FILE).read_bytes(), before)
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_locked_roster_is_read_after_its_writer_finishes(self) -> None:
        self.open_worker()
        roster_path = self.session_dir / codex_mesh.ROSTER_FILE
        original = roster_path.read_bytes()
        script = ("from pathlib import Path; import sys; sys.path.insert(0, sys.argv[2]); "
                  "from scripts.direct import workers; "
                  "print('reader-ready', flush=True); print(workers.resolve(Path(sys.argv[1]), 'impl'), flush=True)")
        with roster_path.open("r+b") as roster:
            fcntl.flock(roster, fcntl.LOCK_EX)
            _ = roster.truncate(0)
            roster.flush()
            child = subprocess.Popen([sys.executable, "-c", script, str(self.session_dir), str(SCRIPT.parents[2])],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
            try:
                assert child.stdout is not None
                ready, _, _ = select.select([child.stdout], [], [], 4)
                self.assertTrue(ready, "reader did not finish importing within its fixture deadline")
                self.assertEqual(child.stdout.readline(), b"reader-ready\n")
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = child.communicate(timeout=0.2)
                _ = roster.seek(0)
                _ = roster.write(original)
                roster.flush()
                fcntl.flock(roster, fcntl.LOCK_UN)
                output, errors = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, errors.decode())
                self.assertEqual(output.decode().strip(), "address-with-no-role-suffix")
            finally:
                if child.poll() is None:
                    child.kill()
                    _ = child.communicate(timeout=4)
        self.assertEqual(self.methods("thread/start"), [])

    def test_legacy_reviewer_instructions_keep_the_read_only_ledger_policy(self) -> None:
        self.open_worker(name="reviewer-address", role="review")
        instructions = self.session_dir / "review.md"
        _ = instructions.write_text("Review only; report findings.")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["reviewer-address"])["instructions_file"] = str(instructions)
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        self.assertEqual(self.resolve(self.session_dir, "review"), "reviewer-address")
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertEqual(ledger.workers["reviewer-address"].access_policy.get("kind"), "ReadOnlyReviewer")

    def test_concurrent_reviews_open_distinct_read_only_unit_workers(self) -> None:
        self.configure_new_worker()
        registry = self.session_dir.parent / "registry.conf"
        with registry.open("a", encoding="utf-8") as handle:
            _ = handle.write("review=gpt-kept-sol:xhigh\n")
        original = self.server.respond
        opened: list[str] = []

        @mesh_fixture.bounded_fixture
        def open_reviewer(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/start":
                identity = f"review-thread-{len(opened) + 1}"
                opened.append(identity)
                return [{"id": request["id"], "result": {"thread": {"id": identity}}}]
            return original(request)

        self.server.respond = open_reviewer
        first = self.resolve(self.session_dir, "review")
        second = self.resolve(self.session_dir, "review")
        self.assertEqual((first, second), ("repository-review", "repository-review2"))
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            for name, thread in zip((first, second), opened, strict=True):
                worker = ledger.workers[name]
                self.assertEqual(worker.role, "review")
                self.assertEqual(worker.backend, workers.CodexThread(thread, os.environ["CODEX_HOME"]))
                self.assertEqual(worker.access_policy.get("kind"), "ReadOnlyReviewer")
                self.assertEqual(worker.ownership, workers.HeldBy(str(self.session_dir)))
        self.assertEqual(len(self.methods("thread/start")), 2)
        for request in self.methods("thread/start"):
            self.assertEqual(cast(dict[str, object], request["params"]).get("sandbox"), "read-only")
        self.assertEqual(self.methods("turn/start"), [])

    def test_missing_identities_and_invalid_events_are_refused_when_loading_the_ledger(self) -> None:
        self.open_worker()
        name = self.resolve(self.session_dir)
        store = workers.ledger_for_run(self.session_dir)
        original = store.path.read_text()
        invalid_fields: tuple[tuple[str, object], ...] = (
            ("ownership", {"kind": "HeldBy"}),
            ("ownership", {"kind": "Adopted", "to_run": ""}),
            ("backend", {"kind": "CodexThread", "codex_home": os.environ["CODEX_HOME"]}),
            ("backend", {"kind": "CodexThread", "thread_id": mesh_fixture.THREAD_ID, "codex_home": ""}),
            ("backend", {"kind": "ClaudeSession", "session_id": ""}),
            ("events", [{"kind": "opened", "run": str(self.session_dir), "at": 1}]),
            ("events", [{"kind": "unknown", "cause": "invalid event", "run": str(self.session_dir), "at": 1}]),
        )
        for field, variant in invalid_fields:
            with self.subTest(field=field, variant=variant):
                data = cast(dict[str, object], json.loads(original))
                entries = cast(dict[str, object], data["workers"])
                cast(dict[str, object], entries[name])[field] = variant
                _ = store.path.write_text(json.dumps(data))
                with self.assertRaises(workers.WorkerUnavailable):
                    with store.locked():
                        self.fail("invalid persisted identity was accepted")

    def test_resolve_reuses_the_stored_role_and_live_model_identity(self) -> None:
        self.open_worker()
        self.assertEqual(self.resolve(self.session_dir), "address-with-no-role-suffix")
        record = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        worker = cast(dict[str, object], record["address-with-no-role-suffix"])
        self.assertEqual(worker["thread_id"], mesh_fixture.THREAD_ID)
        self.assertEqual(worker["model"], "gpt-kept-sol")
        self.assertEqual(self.methods("thread/start"), [])

    def resolve_identity(self, model: str, effort: str) -> str:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "resolve", "--session-dir", str(self.session_dir),
             "--role", "impl", "--owner-pid", str(os.getpid()),
             "--model", model, "--effort", effort],
            cwd=self.session_dir.parent / "repository", capture_output=True, text=True,
            env=os.environ.copy(), timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1, result.stdout)
        return result.stdout.strip()

    def test_explicit_model_and_effort_select_an_exact_idle_identity(self) -> None:
        self.open_worker("keep-first")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        original = cast(dict[str, object], roster["keep-first"])
        other = {**original, "thread_id": "other-thread", "model": "gpt-other-sol", "effort": "high"}
        roster["select-this"] = other
        self.write_pool_roster(roster)

        self.assertEqual(self.resolve_identity("gpt-other-sol", "high"), "select-this")

        after = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        self.assertEqual(cast(dict[str, object], after["keep-first"]), original)
        self.assertEqual(self.methods("thread/start"), [])
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertEqual((ledger.workers["select-this"].model, ledger.workers["select-this"].effort),
                             ("gpt-other-sol", "high"))

    def test_an_effort_override_opens_another_worker_without_mutating_the_unit_identity(self) -> None:
        self.configure_new_worker()
        store = self.lifecycle_worker("impl")
        original = json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"]

        chosen = self.resolve_identity("gpt-kept-sol", "high")

        self.assertEqual(chosen, "impl2")
        self.assertEqual(json_object(self.session_dir / codex_mesh.ROSTER_FILE)["impl"], original)
        with store.locked() as ledger:
            self.assertEqual(ledger.workers["impl"].effort, "xhigh")
            self.assertEqual(ledger.workers[chosen].effort, "high")
            self.assertEqual(ledger.workers[chosen].model, "gpt-kept-sol")
        params = cast(dict[str, object], self.methods("thread/start")[0]["params"])
        self.assertEqual(params["model"], "gpt-kept-sol")

    def test_shown_context_follows_transfer_and_rename_while_the_address_stays_stable(self) -> None:
        _ = self.lifecycle_worker("w7")

        def shown() -> str:
            result = subprocess.run([sys.executable, str(SCRIPT), "shown-name", "--session-dir",
                                     str(self.session_dir), "--name", "w7"],
                                    capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout.strip()

        before = shown()
        self.assertTrue(before.startswith("before · builders · "), before)
        self.assertNotIn("w7", before)
        workers.move_unit("before", "after", "builders", runs=(self.session_dir,))
        self.assertEqual(shown(), before.replace("before ·", "after ·", 1))
        workers.rename_unit("after", "builders", "renamed", runs=(self.session_dir,))
        self.assertEqual(shown(), before.replace("before · builders ·", "after · renamed ·", 1))
        self.assertEqual(self.resolve(self.session_dir), "w7")

    def test_the_unit_ledger_retains_backend_access_model_and_a_caused_open_event(self) -> None:
        self.open_worker()
        name = self.resolve(self.session_dir)
        store = workers.ledger_for_run(self.session_dir)
        self.assertEqual(store.path.parent.name, "adhoc")
        self.assertTrue(store.path.is_relative_to(self.session_dir.parent / "unit-workers"))
        with store.locked() as ledger:
            worker = ledger.workers[name]
            self.assertEqual(worker.role, "impl")
            self.assertEqual(worker.family, "codex")
            self.assertEqual(worker.model, "gpt-kept-sol")
            self.assertEqual(worker.effort, "xhigh")
            self.assertEqual(worker.backend, workers.CodexThread(
                mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
            self.assertEqual(worker.ownership, workers.HeldBy(str(self.session_dir)))
            self.assertEqual(worker.access_policy.get("kind"), "WorkerLaunchPolicy")
            opened = [event for event in worker.events if event.kind == "opened"]
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].cause)
        self.assertFalse((self.session_dir / "workers").exists(),
                         "the unit ledger must not overwrite the launch log")

    def test_adoption_recovers_every_durable_write_before_the_next_command_on_either_run(self) -> None:
        for stage in ("intent", "source", "destination", "ledger", "complete"):
            for command in ("adopt", "resolve", "follow"):
                for side in ("source", "destination"):
                    with self.subTest(stage=stage, command=command, side=side), bounded_worker_change():
                        self.assert_recovered_adoption(stage, command, side)

    def assert_recovered_adoption(self, stage: str, command: str, side: str) -> None:
        identifier = f"{stage}-{command}-{side}"
        source = self.next_run(identifier + "-source")
        destination = self.next_run(identifier + "-destination")
        # Each scenario owns its ledger: an earlier successful recovery must
        # not reserve a worker needed by a later crash scenario.
        context = {"status": "active", "production": "recovery-fixture", "unit": identifier,
                   "working_dir": str(self.session_dir.parent / "repository")}
        for run in (source, destination):
            _ = (run / workers.STATE).write_text(json.dumps(context))
        _ = (source / codex_mesh.ROSTER_FILE).write_text(json.dumps({"worker": {
            "thread_id": mesh_fixture.THREAD_ID, "role": "impl", "status": "done",
            "model": "gpt-kept-sol", "codex_home": os.environ["CODEX_HOME"],
            "port": self.server.port,
        }}))
        reached: list[str] = []

        def interrupt(write: str) -> None:
            reached.append(write)
            if write == stage:
                raise AdoptionWriteInterrupted(write)

        with observe_adoption_writes(source, destination, interrupt):
            with self.assertRaises(AdoptionWriteInterrupted):
                _ = workers.adopt(source, destination, "worker")
        self.assertIn(stage, reached)
        recovering = source if side == "source" else destination
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            if command == "resolve":
                # Recovery runs before resolution can adopt back into the
                # source or allocate another worker. Isolate that later choice
                # so this probe checks the interrupted transaction alone.
                with (patch.object(codex_mesh, "open_unit_worker",
                                   side_effect=workers.WorkerUnavailable("no allocation in recovery probe")),
                      patch.object(workers, "_adopt",
                                   side_effect=workers.WorkerUnavailable("no new adoption in recovery probe"))):
                    try:
                        _ = workers.resolve(recovering, "impl")
                    except workers.WorkerUnavailable:
                        pass
            elif command == "adopt":
                third = self.next_run(identifier + "-third")
                _ = (third / workers.STATE).write_text(json.dumps(context))
                # A fresh adoption command must recover the interrupted
                # worker even if its requested name does not exist.
                with self.assertRaises(workers.WorkerUnavailable):
                    _ = workers.adopt(recovering, third, "missing-worker")
            else:
                try:
                    _ = codex_mesh.main(["follow", "--session-dir", str(recovering),
                                         *self.follow_cli()[1:]])
                except SystemExit:
                    pass
        source_record = cast(dict[str, object], json_object(source / codex_mesh.ROSTER_FILE)["worker"])
        target_record = cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)["worker"])
        self.assertEqual(source_record["adopted_to"], str(destination))
        self.assertEqual(target_record["thread_id"], mesh_fixture.THREAD_ID)
        store = workers.ledger_for_run(destination)
        data = json_object(store.path)
        self.assertEqual(data["adoptions"], {}, "recovery left an incomplete handoff")
        with store.locked() as ledger:
            owner = ledger.workers["worker"].ownership
            self.assertIn(owner, (workers.Adopted(str(destination)), workers.HeldBy(str(destination))))
            events = [event for event in ledger.workers["worker"].events if event.kind == "adopted"]
            self.assertEqual(len(events), 1, "recovery duplicated the handoff event")
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_new_run_adopts_the_unit_thread_and_fences_its_previous_roster(self) -> None:
        self.open_worker()
        name = self.resolve(self.session_dir)
        self.complete_assignment(name)
        destination = self.next_run("next-run")
        self.assertEqual(self.resolve(destination), name)
        source = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)[name])
        target = cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)[name])
        self.assertEqual(source["adopted_to"], str(destination))
        self.assertEqual(target["thread_id"], mesh_fixture.THREAD_ID)
        self.assertEqual(target["model"], "gpt-kept-sol")
        self.assertEqual(self.methods("thread/start"), [])

    def test_plan_unit_identity_survives_the_worktree_move(self) -> None:
        old = self.session_dir.parent / "repository"
        plan = old / "plan.md"
        _ = plan.write_text('> **Production: demo** — unit `builders`; plan\n')
        state = json_object(self.session_dir / workers.STATE)
        state["plan_doc"] = str(plan)
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        self.open_worker("impl")
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        workers.pause_unit("demo", "builders")
        new = old.with_name("moved-repository")
        _ = old.rename(new)
        destination = self.next_run("after-worktree-move")
        state = json_object(destination / workers.STATE)
        state.update({"working_dir": str(new), "plan_doc": str(new / "plan.md")})
        _ = (destination / workers.STATE).write_text(json.dumps(state))
        self.assertEqual(workers.adopt(self.session_dir, destination, "impl"), "impl")
        self.assertEqual(workers.ledger_for_run(self.session_dir).path,
                         workers.ledger_for_run(destination).path)

    def test_moved_paused_worker_resumes_in_a_new_run_with_its_thread_and_home(self) -> None:
        old = self.session_dir.parent / "repository"
        plan = old / "plan.md"
        _ = plan.write_text('> **Production:** demo — unit `builders`; plan\n')
        state = json_object(self.session_dir / workers.STATE)
        state.update({"plan_doc": str(plan), "project_plan_doc": str(plan)})
        _ = (self.session_dir / workers.STATE).write_text(json.dumps(state))
        self.open_worker("impl")
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        workers.pause_unit("demo", "builders")
        roster = json_object(self.session_dir / workers.ROSTER)
        cast(dict[str, object], roster["impl"]).update({"status": "paused", "cwd": str(old)})
        _ = (self.session_dir / workers.ROSTER).write_text(json.dumps(roster))
        original_home = os.environ["CODEX_HOME"]
        new = old.with_name("moved-repository")
        _ = old.rename(new)
        with patch.dict(os.environ, {"UNIT_RUN_ROOT": str(self.session_dir.parent)}):
            found = workers.find_moved_unit_runs((workers.UnitWorktree("demo", "builders", new, "new-branch"),), ())
            self.assertEqual(len(found.moves), 1)
            changes = workers.repoint_moved_unit_runs(found.moves[0], from_production="demo")
            self.assertTrue(changes)
            before = {path: path.read_bytes() for path in self.session_dir.rglob("*") if path.is_file()}
            self.assertEqual(workers.repoint_unit_worktree(
                "demo", "builders", old, new, "new-branch", from_production="demo"), ())
            self.assertEqual({path: path.read_bytes() for path in before}, before)
        state = json_object(self.session_dir / workers.STATE)
        self.assertEqual((state["working_dir"], state["plan_doc"], state["project_plan_doc"]),
                         (str(new), str(new / "plan.md"), str(new / "plan.md")))
        record = cast(dict[str, object], json_object(self.session_dir / workers.ROSTER)["impl"])
        self.assertEqual(record["cwd"], str(new))
        self.assertEqual(record["status"], "paused")
        with workers.ledger_for_run(self.session_dir).locked() as ledger:
            self.assertIsInstance(ledger.workers["impl"].ownership, workers.Paused)
        destination = self.next_run("after-path-repair")
        state = json_object(destination / workers.STATE)
        state.update({"working_dir": str(new), "plan_doc": str(new / "plan.md")})
        _ = (destination / workers.STATE).write_text(json.dumps(state))
        self.assertEqual(workers.adopt(self.session_dir, destination, "impl"), "impl")
        prompt = destination / "continue.txt"
        _ = prompt.write_text("Continue after the unit move.")
        with bounded_worker_change(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.main([
                "follow", "--session-dir", str(destination), "--to", "impl", "--role", "impl",
                "--message-file", str(prompt), "--summary-file", str(destination / "summary.txt"),
                "--log-file", str(destination / "impl.log"), "--timeout", "5",
            ])
        self.assertEqual(code, 0, errors.getvalue())
        resumed = self.methods("thread/resume")
        self.assertTrue(resumed)
        self.assertTrue(all(cast(dict[str, object], request["params"])["threadId"] == mesh_fixture.THREAD_ID
                            for request in resumed))
        with workers.ledger_for_run(destination).locked() as ledger:
            self.assertEqual(ledger.workers["impl"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, original_home))
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_message_arriving_after_adoption_intent_moves_once_to_the_new_owner(self) -> None:
        self.open_worker(name="worker")
        destination = self.next_run("message-race")
        pending = self.session_dir / "worker.pending.json"

        def enqueue_after_intent(stage: str) -> None:
            if stage == "intent":
                _ = pending.write_text(json.dumps({"thread_id": mesh_fixture.THREAD_ID,
                    "messages": ["Deliver this exactly once."], "accepting_messages": True}))

        with bounded_worker_change(), observe_adoption_writes(self.session_dir, destination, enqueue_after_intent):
            self.assertEqual(workers.adopt(self.session_dir, destination, "worker"), "worker")
        self.assertEqual(json_object(pending)["messages"], [])
        target = destination / "worker.pending.json"
        self.assertEqual(json_object(target)["messages"], ["Deliver this exactly once."])
        source = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["worker"])
        self.assertEqual(source["adopted_to"], str(destination))
        self.assertEqual(cast(dict[str, object], json_object(destination / codex_mesh.ROSTER_FILE)["worker"])["thread_id"], mesh_fixture.THREAD_ID)
        with bounded_worker_change():
            workers.recover_adoptions(destination)
        self.assertEqual(json_object(target)["messages"], ["Deliver this exactly once."])
        data = json_object(workers.ledger_for_run(self.session_dir).path)
        self.assertEqual(data["adoptions"], {})

    def test_conflicting_recovery_preserves_the_new_owner_and_unblocks_other_work(self) -> None:
        self.open_worker(name="worker")
        destination = self.next_run("recovery-conflict")

        def interrupted(stage: str) -> None:
            if stage == "source":
                raise AdoptionWriteInterrupted(stage)

        with bounded_worker_change(), observe_adoption_writes(self.session_dir, destination, interrupted):
            with self.assertRaises(AdoptionWriteInterrupted):
                _ = workers.adopt(self.session_dir, destination, "worker")
        concurrent = {"worker": {"thread_id": "concurrent-owner-thread", "status": "done",
                                 "role": "review", "revision": 99}}
        destination_roster = destination / codex_mesh.ROSTER_FILE
        _ = destination_roster.write_text(json.dumps(concurrent))
        destination_inbox = destination / "worker.pending.json"
        _ = destination_inbox.write_text(json.dumps({"thread_id": "concurrent-owner-thread",
                                                   "messages": ["Keep the concurrent owner's work."]}))
        before_roster, before_inbox = destination_roster.read_bytes(), destination_inbox.read_bytes()
        result = subprocess.run([
            sys.executable, str(Path(codex_mesh.__file__)), "follow", "--session-dir", str(self.session_dir),
            *self.follow_cli()[1:],
        ], capture_output=True, text=True, timeout=15, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("quarantin", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(destination_roster.read_bytes(), before_roster)
        self.assertEqual(destination_inbox.read_bytes(), before_inbox)
        source = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)["worker"])
        self.assertNotIn("adopted_to", source)
        data = json_object(workers.ledger_for_run(self.session_dir).path)
        self.assertEqual(data["adoptions"], {})
        self.assertTrue(data.get("quarantined_adoptions"))

        with bounded_worker_change():
            workers.recover_adoptions(destination)
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        roster["other-worker"] = {"thread_id": mesh_fixture.THREAD_ID, "status": "done", "role": "test",
                                  "codex_home": os.environ["CODEX_HOME"], "model": "gpt-kept-sol"}
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        self.assertEqual(self.resolve(self.session_dir, "test"), "other-worker")

    def test_a_truncated_adoption_intent_is_quarantined_once_and_does_not_poison_resolution(self) -> None:
        self.open_worker()
        name = self.resolve(self.session_dir)
        self.complete_assignment(name)
        store = workers.ledger_for_run(self.session_dir)
        data = json_object(store.path)
        data["adoptions"] = {"truncated-intent": {"id": "truncated-intent"}}
        _ = store.path.write_text(json.dumps(data))
        with bounded_worker_change(), self.assertRaises(workers.WorkerUnavailable) as failure:
            workers.recover_adoptions(self.session_dir)
        self.assertIn("quarantin", str(failure.exception).lower())
        self.assertEqual(self.resolve(self.session_dir), name)
        after = json_object(store.path)
        self.assertEqual(after["adoptions"], {})
        self.assertIn("truncated-intent", cast(dict[str, object], after["quarantined_adoptions"]))
        self.assertEqual(self.methods("thread/start"), [])

    def test_a_damaged_ledger_worker_is_reported_once_and_healthy_follow_and_reopen_continue(self) -> None:
        self.open_worker(name="impl")
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        store = workers.ledger_for_run(self.session_dir)
        data = json_object(store.path)
        entries = cast(dict[str, object], data["workers"])
        healthy = entries["impl"]
        damaged = {**cast(dict[str, object], healthy),
                   "backend": {"kind": "CodexThread", "codex_home": os.environ["CODEX_HOME"]}}
        entries["damaged-worker"] = damaged
        _ = store.path.write_text(json.dumps(data))

        args = self.start_args()
        args.to = "impl"
        args.message_file = str(self.session_dir / "prompt.txt")
        with bounded_worker_change(), redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.command_follow(args)
        self.assertEqual(code, 2, errors.getvalue())
        self.assertIn("damaged-worker", errors.getvalue())
        self.assertIn("quarantin", errors.getvalue().lower())
        after = json_object(store.path)
        self.assertEqual(cast(dict[str, object], after["workers"])["impl"], healthy)
        self.assertNotIn("damaged-worker", cast(dict[str, object], after["workers"]))
        quarantine = cast(dict[str, object], after["quarantined_workers"])
        saved = cast(dict[str, object], quarantine["damaged-worker"])
        self.assertEqual(saved["record"], damaged, "quarantine lost the undecodable persisted entry")
        self.assertTrue(saved["reason"])

        self.complete_assignment("impl")
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        cast(dict[str, object], roster["impl"])["status"] = "ended"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        with bounded_worker_change(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = codex_mesh.main(["reopen", "--session-dir", str(self.session_dir), "--to", "impl"])
        self.assertEqual(code, 0, errors.getvalue())
        with bounded_worker_change():
            workers.recover_adoptions(self.session_dir)
        final = json_object(store.path)
        self.assertEqual(final["quarantined_workers"], quarantine)
        with store.locked() as ledger:
            self.assertEqual(ledger.workers["impl"].backend,
                             workers.CodexThread(mesh_fixture.THREAD_ID, os.environ["CODEX_HOME"]))
        self.assertEqual(self.methods("thread/start"), [])

    def test_reactivated_run_reopens_its_worker_on_the_same_thread(self) -> None:
        (self.session_dir / workers.STATE).unlink()
        working = self.session_dir.parent / "repository"
        plan = working / "plan.md"
        _ = plan.write_text("## Delegation Context\n\n- **Project:** Worker recovery\n" +
                            "- **Project started:** 2026-10-28T12:00:00+00:00\n\n" +
                            "## Phases\n\n### Phase 4 — Carry workers · status: todo\n")
        environment = {**os.environ, "UNIT_HISTORY_DIR": str(self.session_dir.parent / "history"),
                       "UNIT_ACTIVE_DIR": str(self.session_dir.parent / "active"),
                       "UNIT_NOW_EPOCH": "1793200000", "UNIT_PASS_OWNER": "launcher"}
        _ = invoke_recorder([
            "start-run", "--session-dir", str(self.session_dir), "--working-dir", str(working),
            "--plan-doc", str(plan), "--main-model", "fixture-director", "--main-family", "codex",
            "--main-session-id", "fixture-director",
        ], environment, working, check=True)
        self.open_worker()
        name = self.resolve(self.session_dir)
        self.complete_assignment(name)
        _ = invoke_recorder(["finish-run", "--session-dir", str(self.session_dir),
                             "--status", "completed"], environment, working, check=True)
        roster = json_object(self.session_dir / codex_mesh.ROSTER_FILE)
        record = cast(dict[str, object], roster[name])
        record["status"] = "ended"
        _ = (self.session_dir / codex_mesh.ROSTER_FILE).write_text(json.dumps(roster))
        marker = self.session_dir / f"{name}.end"
        marker.touch()
        _ = invoke_recorder([
            "start-phase", "--session-dir", str(self.session_dir), "--phase-id", "4",
            "--phase-title", "Carry workers",
        ], environment, working, check=True)
        self.assertEqual(json_object(self.session_dir / workers.STATE)["status"], "active")
        self.assertEqual(self.resolve(self.session_dir), name)
        after = cast(dict[str, object], json_object(self.session_dir / codex_mesh.ROSTER_FILE)[name])
        self.assertEqual(after["thread_id"], mesh_fixture.THREAD_ID)
        self.assertNotEqual(after["status"], "ended")
        self.assertFalse(marker.exists())
        self.complete_assignment(name)
        self.assertTrue(self.methods("thread/resume"))
        self.assertTrue(all(cast(dict[str, object], request["params"])["threadId"] == mesh_fixture.THREAD_ID
                            for request in self.methods("thread/resume")))
        self.assertEqual(self.methods("thread/start"), [])

    def test_racing_run_resolutions_keep_one_existing_thread_and_allocate_a_second_worker(self) -> None:
        self.open_worker(name="impl")
        self.assertEqual(self.resolve(self.session_dir), "impl")
        self.complete_assignment("impl")
        destinations = [self.next_run("racing-a"), self.next_run("racing-b")]
        self.configure_new_worker()
        gate = threading.Barrier(2, timeout=4)

        def resolve_after_gate(run: Path) -> str:
            _ = gate.wait()
            return self.resolve(run)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(resolve_after_gate, run) for run in destinations]
            names = [future.result(timeout=20) for future in futures]
        self.assertEqual(set(names), {"impl", "impl2"})
        threads: list[object] = []
        for run, name in zip(destinations, names):
            entry = cast(dict[str, object], json_object(run / codex_mesh.ROSTER_FILE)[name])
            threads.append(entry["thread_id"])
        self.assertEqual(set(threads), {mesh_fixture.THREAD_ID, "second-thread"})
        self.assertEqual(len(self.methods("thread/start")), 1)


if __name__ == "__main__":
    _ = unittest.main()
