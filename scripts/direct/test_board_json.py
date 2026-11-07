"""The board contract is generated from disposable production and run records."""

from __future__ import annotations

from scripts.shutdown import remote

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, recorded_unit

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.direct import board_json
from scripts.lib import unit_runs
from scripts.production import fake_showrunner, showrunner_parent, staff, showrunners
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import StaffTestCase, fixture_worker, isolated_staff_file, production_membership, showrunner_launch
from scripts.lib.atomic_file import write_text_atomic
from contextlib import redirect_stderr
import io
from scripts.herdr import worker_panes
from scripts.herdr import herdr
from scripts.shutdown import pane_record
from scripts.production import display_cells


CONTRACT = Path(__file__).resolve().parents[2] / "mods/unit-board/types/board.d.ts"
NOW = int(datetime(2026, 10, 16, 18, tzinfo=UTC).timestamp())


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"expected a JSON object, got {value!r}")
    return cast(dict[str, object], value)


def array_items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise AssertionError(f"expected a JSON array, got {value!r}")
    return cast(list[object], value)


@dataclass(frozen=True)
class ProductionFixture:
    name: str
    checkout: Path
    worktree: Path
    plan: Path
    document: Path


class BoardJsonTests(unittest.TestCase):
    def test_invalid_run_pin_keeps_build_and_main_available_with_a_problem_worker(self) -> None:
        fixture = self.production()
        history = self.write_run(fixture, self.delivery_events())
        run = self.session_files()
        parent = self.unit_member(fixture.name)
        worker = fixture_worker("reader", parent.id)
        assert isinstance(worker.parent, staff.ReportsTo)
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: worker.parent.id}))
        write_text_atomic(run / "worker_transport", "junk\n")
        error = io.StringIO()
        with self.source_readers(), patch.dict(os.environ, self.environment), patch.object(
                unit_runs, "run_roots", return_value=(self.run_root, self.former_root)), redirect_stderr(error):
            subtree = board_json.resolve(fixture.name)
            self.assertIsInstance(subtree, board_json.BoardSubtree)
            assert isinstance(subtree, board_json.BoardSubtree)
            document = board_json.build(subtree.runners[0].runner, record=subtree.record)
        units = array_items(document["units"])
        rows = array_items(object_fields(units[0])["workers"])
        self.assertEqual(object_fields(rows[0])["state"], "incomplete")
        self.assertIn("invalid worker transport: junk", str(object_fields(rows[0])["doing"]))
        self.assertIn(str(run / "worker_transport"), error.getvalue())
        self.assertIn(str(history), array_items(document["sources"]))
        result = self.invoke("--production", fixture.name)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("invalid worker transport: junk", result.stderr)
        cached = object_fields(cast(object, json.loads(self.cache_path(fixture.name).read_text())))
        cached_rows = array_items(object_fields(array_items(cached["units"])[0])["workers"])
        self.assertEqual(object_fields(cached_rows[0])["state"], "incomplete")

    def test_wrapped_child_is_not_a_board_descendant_even_when_its_timer_and_parent_remain(self) -> None:
        root = self.production("root")
        child = self.production("wrapped", running=False)
        self.attach(child.name, root.name)
        old = next(row for row in staff.find(kind="showrunner") if row.name == child.name)
        _ = staff.set_doc(old.id, staff.WrappedDoc(remote.local_machine(), child.document))
        _, documents = self.generate("--production", root.name)
        self.assertEqual([document["production"] for document in documents], [root.name])
        retained = staff.by_id(old.id)
        assert retained is not None
        self.assertEqual(retained.parent, old.parent)

    root: Path = Path()
    history: Path = Path()
    run_root: Path = Path()
    former_root: Path = Path()
    notifier: Path = Path()
    showrunner_state: Path = Path()
    sessions: Path = Path()
    session_script: Path = Path()
    output: Path = Path()
    environment: dict[str, str] = {}
    production_count: int = 0

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / "config"
        config.mkdir()
        write_text_atomic(config / "direct.conf", "# one of: mesh, herdr\nWORKER_TRANSPORT=mesh\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(config)})))
        self.history = self.root / "history"
        (self.history / "runs").mkdir(parents=True)
        self.run_root = self.root / "unit"
        self.former_root = self.root / "delegate"
        self.run_root.mkdir()
        self.former_root.mkdir()
        self.notifier = self.root / "notifier"
        self.showrunner_state = self.root / "showrunners"
        self.sessions = self.root / "sessions"
        self.notifier.mkdir()
        self.sessions.mkdir()
        self.output = self.root / "board"
        self.production_count = 0
        self.session_script = self.root / "sessions.py"
        _ = self.session_script.write_text(
            "\n".join((
                "import json, os, pathlib, sys",
                "for path in pathlib.Path(os.environ['NOTIFIER_SESSIONS_DIR']).glob('*.json'):",
                " record = json.loads(path.read_text())",
                " if sys.argv[1:] == ['socket', 'session:' + record['sessionId']]:",
                "  print(record['messagingSocketPath'])",
                "  raise SystemExit(0)",
                "raise SystemExit(1)", "",
            )),
            encoding="utf-8",
        )
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "USER_ZONE": "UTC",
            "UNIT_HISTORY_DIR": str(self.history),
            "UNIT_NOW_EPOCH": str(NOW),
            "UNIT_SYNC_WRITES": "0",
            "PHASE_TABLE_VAULT": str(self.root / "vault"),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "SHOWRUNNERS_SESSIONS": str(self.session_script),
            "SHOWRUNNER_STATE_DIR": str(self.showrunner_state),
            "UNIT_BOARD_DIR": str(self.output),
            "XDG_STATE_HOME": str(self.root / "state"),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "style-ledgers"),
            "HERDR_PANE_RECORD_DIR": str(self.root / "panes"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        production_membership("fixture")

    def production(self, name: str = "show", *, running: bool = True,
                   unit_name: str = "alpha") -> ProductionFixture:
        checkout = self.root / f"{name}-checkout"
        worktree = self.root / f"{name}-unit"
        checkout.mkdir()
        worktree.mkdir()
        _ = subprocess.run(["git", "init", "-b", "branch", "--separate-git-dir", str(self.root / f"{name}-unit-git"), str(worktree)], check=True, capture_output=True)
        plan = worktree / "plan.md"
        document = checkout / f"{name}-document.md"
        _ = document.write_text("\n".join((
            f"# Production — {name}", "", "## Production Context", "",
            "- **Merge branch:** main",
            f"- **Showrunner checkout:** {checkout}",
            "- **Log:** show.log", "- **User zone:** UTC", "",
        )), encoding="utf-8")
        fixture = ProductionFixture(name, checkout, worktree, plan, document)
        self.plan(fixture)
        _ = recorded_unit(name, unit_name, worktree, document, plan="plan.md")
        row = next(row for row in staff.find(kind="showrunner") if row.name == name)
        _ = staff.set_doc(row.id, staff.LiveDoc(remote.local_machine(), document))
        _ = fake_showrunner.write_timer(self.notifier, name, row.id, "UTC", document)
        if running:
            process_ids = (os.getpid(), os.getppid(), 1)
            held = fake_showrunner.write_session(
                self.sessions, name, row.id,
                pid=process_ids[self.production_count],
            )
            self.addCleanup(held.close)
        self.production_count += 1
        showrunner_launch(row, row.id, self.environment)
        return fixture

    def cache_path(self, name: str) -> Path:
        row = next(row for row in staff.find(kind="showrunner") if row.name == name)
        return self.output / f"{row.id}.json"

    def plan(self, fixture: ProductionFixture, *, all_done: bool = False) -> None:
        status = "done" if all_done else "todo"
        _ = fixture.plan.write_text("\n".join((
            f"> **Production: {fixture.name}** — unit `alpha`; production doc `{fixture.document}`",
            "", "### Phase 1 — First delivery  · status: done", "",
            f"### Phase 2 — Current delivery  · status: {status}", "",
            f"### Phase 3 — Next delivery  · status: {status}", "",
            f"### Phase 4 — Last delivery  · status: {status}", "",
        )), encoding="utf-8")

    def attach(self, child: str, parent: str) -> None:
        self.move_parent(child, parent)
        directory = self.showrunner_state / next(row.id for row in staff.find(kind="showrunner") if row.name == child)
        directory.mkdir(parents=True, exist_ok=True)

    def move_parent(self, child: str, parent: str = "") -> None:
        rows = {row.name: row.id for row in staff.find(kind="showrunner")}
        _ = staff.move(rows[child], staff.ReportsTo(rows[parent]) if parent else staff.StaffRoot())

    def staff_launch(self, row: staff.StaffRow, session: str, *,
                     pane: str = "unit", at: str = "2026-10-31T10:00:00+00:00") -> None:
        launch = pane_record.launch(
            pane, session, str(self.root),
            {"CLAUDE_CONFIG_DIR": str(self.root / "account")},
            ["claude", "-n", row.name],
        )
        launch["written_at"] = at
        pane_record.write("agents", launch)

    def unit_member(self, production: str, name: str = "alpha") -> staff.StaffRow:
        parent = next(row for row in staff.find(kind="showrunner") if row.name == production)
        return next(row for row in staff.find(kind="unit", parent=parent.id) if row.name == name)

    def standing_plan(self, fixture: ProductionFixture, *, phase_id: str = "alpha-63",
                      title: str = "Fix names") -> None:
        # A real linked worktree pins the ledger's main-repository namespace,
        # which differs from both the Unit cell and the worktree basename.
        for arguments in (
            ["git", "init", "--quiet", str(fixture.checkout)],
            ["git", "-C", str(fixture.checkout), "-c", "user.name=Fixture",
             "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
             "commit", "--quiet", "--allow-empty", "-m", "Fixture"],
        ):
            result = subprocess.run(arguments, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        fixture.plan.unlink()
        configuration = fixture.worktree / ".unit.toml"
        configuration_text = configuration.read_text(encoding="utf-8")
        configuration.unlink()
        (fixture.worktree / ".git").unlink()
        result = subprocess.run(
            ["git", "-C", str(fixture.checkout), "worktree", "add", "--quiet", "--detach",
             str(fixture.worktree), "HEAD"], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        _ = configuration.write_text(configuration_text, encoding="utf-8")
        _ = fixture.plan.write_text(
            "> **Standing:** style_plan.py\n\n"
            + f"### Phase {phase_id} — {title} · status: todo\n", encoding="utf-8",
        )
        _ = self.write_run(fixture, [self.event(
            "phase_started", NOW - 120, phase=phase_id, instance="ticket-63",
            phase_title=title,
        )])

    def ledger(self, fixture: ProductionFixture, *, default_path: bool = False,
               tickets: tuple[tuple[str, str, str, int], ...] = ()) -> Path:
        root = self.root / ".local/state/style" if default_path else self.root / "style-ledgers"
        database = root / fixture.checkout.name / "ledger.sqlite"
        database.parent.mkdir(parents=True, exist_ok=True)
        parent = next(row for row in staff.find(kind="showrunner") if row.name == fixture.name)
        unit_ids = {row.name: row.id for row in staff.find(kind="unit", parent=parent.id)}
        connection = sqlite3.connect(database)
        try:
            _ = connection.execute(
                "CREATE TABLE tickets (id TEXT, unit TEXT, state TEXT, created_at INTEGER)",
            )
            _ = connection.executemany("INSERT INTO tickets VALUES (?, ?, ?, ?)", [
                (ticket, unit_ids.get(owner, fixture_runtime_id(owner)), state, created_at)
                for ticket, owner, state, created_at in [
                    ("alpha-1", "alpha", "merged", 1),
                    ("alpha-2", "alpha", "merged", 2),
                    ("alpha-3", "alpha", "open", 3),
                    ("alpha-4", "alpha", "rejected", 4),
                    ("other-1", "other", "merged", 5),
                    ("alpha-1", "unrelated", "merged", 6),
                    *tickets,
            ]])
            connection.commit()
        finally:
            connection.close()
        return database

    @staticmethod
    def event(kind: str, at: int, *, phase: str = "2", instance: str = "two",
              **fields: object) -> dict[str, object]:
        return {
            "event_type": kind, "timestamp_epoch": at,
            "phase_id": phase, "phase_instance_id": instance, **fields,
        }

    def write_run(self, fixture: ProductionFixture, events: list[dict[str, object]],
                  *, name: str = "current", started: int = NOW - 7_200) -> Path:
        path = self.history / "runs" / f"{name}.jsonl"
        member = next(row for row in staff.find(kind="unit")
                      if isinstance(row.worktree, staff.PresentWorktree) and row.worktree.path == fixture.worktree)
        first: dict[str, object] = {
            "event_type": "run_started", "working_dir": str(fixture.worktree),
            "main_agent": {"session_id": member.current_session},
            "plan_doc": str(fixture.plan), "project_plan_doc": str(fixture.plan),
            "timestamp_epoch": started, "run_started_at": started,
        }
        _ = path.write_text(
            "\n".join(json.dumps(event) for event in [first, *events]) + "\n",
            encoding="utf-8",
        )
        return path

    def delivery_events(self, *, open_phase: bool = True) -> list[dict[str, object]]:
        events = [
            self.event("phase_started", NOW - 6_120, phase="1", instance="one",
                       phase_title="First delivery"),
            self.event("pass_started", NOW - 6_100, phase="1", instance="one",
                       pass_instance_id="old-build", pass_kind="impl",
                       pass_activity="the previous phase's work"),
            self.event("pass_finished", NOW - 5_600, phase="1", instance="one",
                       pass_instance_id="old-build", pass_kind="impl",
                       status="completed", pass_elapsed_seconds=500),
            self.event("phase_finished", NOW - 5_520, phase="1", instance="one",
                       status="completed", phase_elapsed_seconds=600),
        ]
        if open_phase:
            events += [self.event("phase_started", NOW - 5_400, phase_title="Current delivery")]
        return events

    def session_files(self, name: str = "current", *, former: bool = False) -> Path:
        directory = (self.former_root if former else self.run_root) / name
        directory.mkdir(exist_ok=True)
        write_text_atomic(directory / "worker_transport", "mesh\n")
        _ = (directory / "mesh_roster.json").write_text(json.dumps({
            "alpha-impl": {"slot": "1", "role": "impl", "status": "running",
                           "thread_id": "thread-one", "turn_id": "turn-one",
                           "model": "legacy-identity", "effort": "low",
                           "turn_model": "fixture-turn-model", "turn_effort": "high"},
            "alpha-test": {"slot": "test", "role": "test", "status": "idle",
                           "thread_id": "thread-test", "turn_id": "turn-test"},
        }), encoding="utf-8")
        _ = (directory / "impl_worker_1.json").write_text(json.dumps({
            "name": "alpha-impl", "slot": "1", "family": "codex",
            "model": "fixture-model", "worker_id": "thread-one",
        }), encoding="utf-8")
        _ = (directory / "board.log").write_text(
            "\n".join((
                "2026-10-16T17:00:00Z [alpha-impl] status: obsolete task",
                "2026-10-16T17:01:00Z [alpha-test] status: checking the delivery",
                "2026-10-16T17:02:00Z [alpha-impl] status: building " + "x" * 100, "",
            )),
            encoding="utf-8",
        )
        return directory

    @contextmanager
    def source_readers(self) -> Generator[None]:
        with ExitStack() as scope:
            # Both import spellings occur in production scripts; rebind their
            # import-time path constants, without replacing the registry reader.
            for name in ("showrunners", "scripts.production.showrunners"):
                module = sys.modules.get(name)
                if module is not None:
                    for field, value in (
                        ("NOTIFIER_STATE_DIR", self.notifier),
                        ("SESSIONS", self.session_script), ("SHOWRUNNER_STATE_DIR", self.showrunner_state),
                    ):
                        _ = scope.enter_context(patch.object(module, field, value))
            yield

    def invoke(self, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        with self.source_readers(), patch.object(
                unit_runs, "run_roots", return_value=(self.run_root, self.former_root)):
            return invoke_cli(
                board_json, lambda: board_json.main(list(arguments)), list(arguments),
                self.environment, cwd if cwd is not None else self.root,
            )

    def generate(self, *arguments: str, cwd: Path | None = None) -> tuple[dict[str, object], list[dict[str, object]]]:
        result = self.invoke(*arguments, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        response = object_fields(cast(object, json.loads(result.stdout)))
        documents: list[dict[str, object]] = []
        for value in array_items(response["files"]):
            self.assertIsInstance(value, str)
            documents.append(object_fields(cast(object, json.loads(Path(str(value)).read_text()))))
        return response, documents

    def unit(self, fixture: ProductionFixture) -> tuple[dict[str, object], dict[str, object]]:
        _, documents = self.generate("--production", fixture.name)
        self.assertEqual(len(documents), 1)
        units = array_items(documents[0]["units"])
        self.assertEqual(len(units), 1)
        return documents[0], object_fields(units[0])

    def test_unit_session_selects_its_direct_showrunner_from_its_worktree(self) -> None:
        fixture = self.production()
        other = self.production("other")
        nested = fixture.worktree / "nested"
        nested.mkdir()
        unit = self.unit_member(fixture.name)
        self.staff_launch(unit, unit.id)

        response, documents = self.generate("--session", unit.id, cwd=nested)

        self.assertEqual(response["files"], [str(self.cache_path("show"))])
        self.assertNotIn("focus", response)
        self.assertEqual([document["production"] for document in documents], [fixture.name])
        self.assertFalse(self.cache_path(other.name).exists())

    def test_showrunner_session_and_production_select_the_same_three_level_subtree(self) -> None:
        root = self.production("z-root")
        child = self.production("m-child")
        grandchild = self.production("a-grandchild")
        other = self.production("other", running=False)
        self.attach(child.name, root.name)
        self.attach(grandchild.name, child.name)

        for arguments in (("--session", fixture_runtime_id("showrunner::z-root")), ("--production", root.name)):
            with self.subTest(arguments=arguments):
                response, documents = self.generate(*arguments)

                self.assertEqual(response["files"], [str(self.cache_path(name))
                                                     for name in (root.name, child.name, grandchild.name)])
                self.assertEqual([doc["production"] for doc in documents],
                                 [root.name, child.name, grandchild.name])
                self.assertNotIn("focus", response)
                self.assertEqual(response["watch"], sorted(str(path.resolve()) for path in (
                    self.notifier, self.showrunner_state,
                    self.showrunner_state / fixture_runtime_id(f"showrunner::{child.name}"), self.showrunner_state / fixture_runtime_id(f"showrunner::{grandchild.name}"),
                )))
                for fixture, document in zip((root, child, grandchild), documents, strict=True):
                    unit = object_fields(array_items(document["units"])[0])
                    self.assertEqual(unit["key"], "alpha")
                    self.assertEqual(unit["plan"], str(fixture.plan))
                self.assertFalse(self.cache_path(other.name).exists())

        response, documents = self.generate("--session", fixture_runtime_id("showrunner::m-child"))
        self.assertEqual([doc["production"] for doc in documents], [child.name, grandchild.name])
        self.assertNotIn("focus", response)

    def test_staff_showrunner_session_selects_its_production_from_an_unrelated_cwd(self) -> None:
        fixture = self.production(running=False)
        unrelated = self.root / "etc/nixos"
        unrelated.mkdir(parents=True)

        response, documents = self.generate("--session", fixture_runtime_id("showrunner::show"), cwd=unrelated)

        self.assertEqual(response["files"], [str(self.cache_path("show"))])
        self.assertEqual([document["production"] for document in documents], [fixture.name])
        unit = object_fields(array_items(documents[0]["units"])[0])
        self.assertEqual(unit["plan"], str(fixture.plan))

    def test_staff_session_placement_is_unchanged_in_another_productions_cwd(self) -> None:
        fixture = self.production("session-production", running=False)
        other = self.production("cwd-production", running=False)

        for cwd in (self.root, fixture.checkout, other.checkout):
            with self.subTest(cwd=cwd):
                _, documents = self.generate("--session", fixture_runtime_id("showrunner::session-production"), cwd=cwd)
                self.assertEqual([document["production"] for document in documents], [fixture.name])
                self.assertFalse(self.cache_path(other.name).exists())

    def test_showrunner_session_selects_its_subtree_without_parents_or_siblings(self) -> None:
        parent = self.production("parent", running=False)
        root = self.production("z-root", running=False)
        child = self.production("m-child", running=False)
        grandchild = self.production("a-grandchild", running=False)
        sibling = self.production("sibling", running=False)
        self.attach(root.name, parent.name)
        self.attach(sibling.name, parent.name)
        self.attach(child.name, root.name)
        self.attach(grandchild.name, child.name)

        response, documents = self.generate("--session", fixture_runtime_id("showrunner::z-root"))

        self.assertEqual([document["production"] for document in documents],
                         [root.name, child.name, grandchild.name])
        self.assertEqual(response["files"], [str(self.cache_path(name))
                                             for name in (root.name, child.name, grandchild.name)])
        self.assertFalse(self.cache_path(parent.name).exists())
        self.assertFalse(self.cache_path(sibling.name).exists())

    def test_unknown_session_stays_empty_even_in_a_production_checkout(self) -> None:
        fixture = self.production(running=False)
        nested = fixture.checkout / "nested"
        nested.mkdir()
        for cwd in (nested, self.root):
            with self.subTest(cwd=cwd):
                _, documents = self.generate("--session", "unknown-session", cwd=cwd)
                self.assertEqual(documents, [])

    def test_unit_and_worker_sessions_see_only_the_direct_showrunner_subtree(self) -> None:
        parent = self.production("parent", running=False)
        root = self.production("root", running=False)
        child = self.production("child", running=False)
        sibling = self.production("sibling", running=False)
        grandchild = self.production("grandchild", running=False)
        self.attach(root.name, parent.name)
        self.attach(sibling.name, parent.name)
        self.attach(child.name, root.name)
        self.attach(grandchild.name, child.name)
        unit = self.unit_member(root.name)
        worker = fixture_member("worker", name="test1", parent=unit.id)
        self.staff_launch(unit, unit.id)
        self.staff_launch(worker, worker.id, pane="worker")

        for session in (unit.id, worker.id):
            with self.subTest(session=session):
                _, documents = self.generate("--session", session)
                self.assertEqual([doc["production"] for doc in documents],
                                 [root.name, child.name, grandchild.name])
                self.assertFalse(self.cache_path(parent.name).exists())
                self.assertFalse(self.cache_path(sibling.name).exists())

    def test_same_named_units_follow_the_newest_launch_staff_id(self) -> None:
        first = self.production("first", running=False)
        second = self.production("second", running=False)
        unit = self.unit_member(first.name)
        self.staff_launch(unit, unit.id, pane="older", at="2026-10-31T09:00:00+00:00")
        second_parent = next(row for row in staff.find(kind="showrunner") if row.name == second.name)
        other = self.unit_member(second.name)
        _ = staff.rename(other.id, "unrelated")
        _ = staff.set_worktree(other.id, staff.RemovedWorktree(remote.local_machine(), second.worktree), expected=other.worktree)
        moved = staff.move(unit.id, staff.ReportsTo(second_parent.id))
        self.staff_launch(moved, moved.id, pane="newer")

        _, documents = self.generate("--session", moved.id)

        self.assertEqual([doc["production"] for doc in documents], [second.name])
        self.assertFalse(self.cache_path(first.name).exists())

    def test_timer_target_without_a_launch_staff_id_does_not_place_a_session(self) -> None:
        _ = self.production(running=False)
        target = fixture_runtime_id("unregistered-board-timer")
        with pane_record.locked("agents"):
            pane_record.save("agents", {})
        launch = pane_record.launch("unmarked", target, str(self.root),
                                    {"CLAUDE_CONFIG_DIR": str(self.root / "account")}, ["claude"])
        launch["written_at"] = "2099-01-01T00:00:00+00:00"
        pane_record.save("agents", {launch["pane_id"]: launch})

        response, documents = self.generate("--session", target)

        self.assertEqual(response["files"], [])
        self.assertEqual(documents, [])

    def test_missing_staff_id_and_launch_records_have_no_showrunner_board(self) -> None:
        _ = self.production(running=False)
        missing = staff.StaffRow(staff.native_id(fixture_runtime_id("absent-board")), "unit", "alpha", staff.StaffRoot(), current_session=staff.native_id(fixture_runtime_id("absent-board")))
        self.staff_launch(missing, missing.id, pane="missing")

        for session in ("no-launch-session", missing.id):
            with self.subTest(session=session):
                response, documents = self.generate("--session", session)
                self.assertEqual(response["files"], [])
                self.assertEqual(documents, [])

    def test_unit_without_a_showrunner_ancestor_has_no_board(self) -> None:
        _ = self.production(running=False)
        orphan = staff.StaffRow(staff.native_id(fixture_runtime_id("orphan-board")), "unit", "orphan", staff.StaffRoot(), current_session=staff.native_id(fixture_runtime_id("orphan-board")))
        self.staff_launch(orphan, orphan.id)
        # The writer forbids orphan units; pin the reader's defensive boundary
        # with the same snapshot shape should an incomplete ancestry reach it.
        record = staff.snapshot()
        with patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((*record.rows, orphan))):
            response, documents = self.generate("--session", orphan.id)

        self.assertEqual(response["files"], [])
        self.assertEqual(documents, [])

    def test_staff_edges_override_stale_parent_routing_files(self) -> None:
        root = self.production("root", running=False)
        child = self.production("child", running=False)
        sibling = self.production("sibling", running=False)
        self.move_parent(child.name, root.name)
        stale_child = self.showrunner_state / fixture_runtime_id(f"showrunner::{child.name}")
        stale_child.mkdir(parents=True)
        _ = (stale_child / "parent").write_text(sibling.name + "\n", encoding="utf-8")

        _, documents = self.generate("--session", fixture_runtime_id("showrunner::root"))

        self.assertEqual([doc["production"] for doc in documents], [root.name, child.name])
        self.assertFalse(self.cache_path(sibling.name).exists())

    def test_unreadable_staff_prints_one_error_and_no_boards(self) -> None:
        _ = self.production(running=False)
        _ = (self.root / "staff.json").write_text("not json", encoding="utf-8")

        result = self.invoke("--session", "show-id")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("staff", result.stderr)
        response = object_fields(cast(object, json.loads(result.stdout)))
        self.assertEqual(response["files"], [])
        self.assertFalse(self.output.exists())

    def test_board_run_reuses_one_staff_snapshot_after_the_file_disappears(self) -> None:
        root = self.production("root", running=False)
        child = self.production("child", running=False)
        self.move_parent(child.name, root.name)
        snapshot = staff.snapshot
        resolve = board_json.resolve
        reads = 0

        def read_staff() -> staff.StaffSnapshot:
            nonlocal reads
            reads += 1
            return snapshot()

        def resolve_then_remove_staff(
                production: str, session: str = "") -> board_json.BoardSubtree | board_json.NoBoardSubtree:
            resolved = resolve(production, session)
            (self.root / "staff.json").unlink()
            return resolved

        with patch.object(staff, "snapshot", read_staff), patch.object(
                board_json, "resolve", resolve_then_remove_staff):
            _, documents = self.generate("--session", fixture_runtime_id("showrunner::root"))

        self.assertEqual(reads, 1)
        self.assertEqual([document["production"] for document in documents], [root.name, child.name])
        for document in documents:
            units = array_items(document["units"])
            self.assertEqual([object_fields(unit)["key"] for unit in units], ["alpha"])

    def test_cwd_option_is_rejected(self) -> None:
        result = self.invoke("--session", "unknown", "--cwd", str(self.root))

        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --cwd", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_explicit_production_wins_over_session(self) -> None:
        fixture = self.production("explicit", running=False)
        other = self.production("session-production", running=False)
        for production, expected in ((fixture.name, [fixture.name]), ("unknown-production", [])):
            with self.subTest(production=production):
                _, documents = self.generate("--production", production,
                                             "--session", fixture_runtime_id("showrunner::session-production"))

                self.assertEqual([document["production"] for document in documents], expected)
                self.assertFalse(self.cache_path(other.name).exists())

    def test_attach_and_detach_change_a_watched_directory_and_refresh_the_subtree(self) -> None:
        root = self.production("root")
        child = self.production("child")
        child_state = self.showrunner_state / fixture_runtime_id(f"showrunner::{child.name}")
        child_state.mkdir(parents=True)
        # An unrelated directory must not become a discovery source.
        (self.showrunner_state / "unregistered").mkdir()
        expected_watch = sorted(str(path.resolve()) for path in (
            self.notifier, self.showrunner_state, child_state,
        ))
        with self.source_readers(), patch.object(showrunners, "tell_others", return_value=showrunners.NoticeDelivery()):
            for operation in ("attach", "detach"):
                with self.subTest(operation=operation):
                    # Give each actual filesystem operation room to advance mtime,
                    # without sleeping or relying on filesystem clock resolution.
                    os.utime(child_state, (NOW, NOW))
                    response, documents = self.generate("--session", fixture_runtime_id("showrunner::root"))
                    self.assertEqual(response["watch"], expected_watch)
                    watched_mtime = child_state.stat().st_mtime_ns
                    self.assertEqual([doc["production"] for doc in documents],
                                     [root.name] if operation == "attach" else [root.name, child.name])

                    if operation == "attach":
                        self.assertFalse((child_state / "parent").exists())
                        showrunner_parent.attach_parent(child.name, root.name)
                    else:
                        showrunner_parent.detach_parent(child.name)
                    members = {row.name: row for row in staff.find(kind="showrunner")}
                    self.assertEqual(members[child.name].parent,
                                     staff.ReportsTo(members[root.name].id) if operation == "attach" else staff.StaffRoot())
                    self.assertFalse((child_state / "parent").exists())
                    self.assertGreater(child_state.stat().st_mtime_ns, watched_mtime)

                    response, documents = self.generate("--session", fixture_runtime_id("showrunner::root"))
                    self.assertEqual(response["watch"], expected_watch)
                    self.assertEqual([doc["production"] for doc in documents],
                                     [root.name, child.name] if operation == "attach" else [root.name])

    def test_subtree_keeps_nearest_levels_before_grandchildren_and_includes_stopped_showrunners(self) -> None:
        root = self.production("root")
        child = self.production("b-child", running=False)
        sibling = self.production("z-sibling", running=False)
        grandchild = self.production("a-grandchild", running=False)
        self.attach(child.name, root.name)
        self.attach(sibling.name, root.name)
        self.attach(grandchild.name, child.name)

        _, documents = self.generate("--production", root.name)

        self.assertEqual([doc["production"] for doc in documents],
                         [root.name, child.name, sibling.name, grandchild.name])

    def test_partial_discovery_does_not_publish_an_incomplete_subtree(self) -> None:
        root = self.production("root")
        child = self.production("child")
        self.attach(child.name, root.name)
        timer_for = showrunners.timer_for

        def read_timer(row: staff.StaffRow, record: staff.StaffSnapshot) -> showrunners.TimerRegistration | showrunners.NoTimer:
            if row.name == child.name:
                raise OSError("fixture lookup failed")
            return timer_for(row, record)

        with patch.object(showrunners, "timer_for", side_effect=read_timer):
            with self.assertRaisesRegex(OSError, "fixture lookup failed"):
                _ = board_json.main(["--production", root.name])
        self.assertFalse(self.cache_path("root").exists())
        self.assertFalse(self.cache_path("child").exists())

    def test_descendant_not_in_the_registered_showrunners_is_left_out(self) -> None:
        root = self.production("root")
        child = self.production("child")
        grandchild = self.production("grandchild")
        self.attach(child.name, root.name)
        self.attach(grandchild.name, child.name)
        child_row = next(row for row in staff.find(kind="showrunner") if row.name == child.name)
        document = self.root / "unregistered-production.md"
        _ = document.write_text("# Production — unregistered\n")
        _ = fixture_member("showrunner", name="unregistered", parent=child_row.id,
                           doc=staff.LiveDoc(remote.local_machine(), document))
        response, documents = self.generate("--production", root.name)

        self.assertEqual([doc["production"] for doc in documents],
                         [root.name, child.name, grandchild.name])
        self.assertNotIn("focus", response)
        self.assertFalse((self.output / "unregistered.json").exists())

    def test_unknown_explicit_production_does_not_fall_back_to_registered_productions(self) -> None:
        _ = self.production()

        response, documents = self.generate("--production", "unregistered")

        self.assertEqual(response["files"], [])
        self.assertEqual(documents, [])
        self.assertNotIn("focus", response)

    def test_deleted_plan_is_omitted_from_the_unit_path(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        fixture.plan.unlink()

        _, unit = self.unit(fixture)

        self.assertEqual(unit["plan"], "")

    def test_production_as_built_path_replaces_the_deleted_run_plan(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        fixture.plan.unlink()
        as_built = fixture.worktree / "as-built.md"
        _ = as_built.write_text("# Delivered feature\n", encoding="utf-8")

        _ = recorded_unit(fixture.name, "alpha", fixture.worktree, fixture.document, plan="as-built.md")
        _, unit = self.unit(fixture)

        self.assertEqual(unit["plan"], str(as_built))
        self.assertNotEqual(unit["plan"], str(fixture.plan))

    def test_unlisted_session_does_not_fall_back_to_running_productions(self) -> None:
        _ = self.production()
        _ = self.production("other")
        _ = self.production("stopped", running=False)

        response, documents = self.generate("--session", "unlisted-session")

        self.assertNotIn("focus", response)
        self.assertEqual(response["files"], [])
        self.assertEqual(documents, [])
        self.assertFalse(self.cache_path("show").exists())
        self.assertFalse(self.cache_path("other").exists())
        self.assertFalse(self.cache_path("stopped").exists())

    def test_no_registered_production_resolves_without_output_files(self) -> None:
        response, documents = self.generate("--session", "unlisted-session")
        self.assertEqual(response, {
            "files": [], "watch": [str(self.notifier.resolve())],
        })
        self.assertEqual(documents, [])

    def test_missing_registry_produces_no_discovery_watch_paths(self) -> None:
        self.notifier.rmdir()

        response, documents = self.generate("--session", "unlisted-session")

        self.assertEqual(response, {"files": [], "watch": []})
        self.assertEqual(documents, [])

    def test_output_directory_uses_xdg_then_home_when_no_override_is_set(self) -> None:
        fixture = self.production()
        _ = self.environment.pop("UNIT_BOARD_DIR")
        response, _ = self.generate("--production", fixture.name)
        self.assertEqual(response["files"], [str(self.root / "state/unit-board" / self.cache_path("show").name)])
        _ = self.environment.pop("XDG_STATE_HOME")
        response, _ = self.generate("--production", fixture.name)
        self.assertEqual(response["files"], [str(self.root / ".local/state/unit-board" / self.cache_path("show").name)])

    def test_cache_key_survives_rename_transfer_and_session_clear(self) -> None:
        fixture = self.production(running=False)
        parent = self.production("parent", running=False)
        owner = next(row for row in staff.find(kind="showrunner") if row.name == fixture.name)
        parent_row = next(row for row in staff.find(kind="showrunner") if row.name == parent.name)
        before, _ = self.generate("--production", fixture.name)
        _ = staff.rename(owner.id, "renamed")
        _ = staff.move(owner.id, staff.ReportsTo(parent_row.id))
        current = staff.native_id(fixture_runtime_id("cleared-showrunner"))
        _ = staff.update_current_session(owner.current_session, current)
        after, documents = self.generate("--session", current)
        self.assertEqual(after["files"], before["files"])
        self.assertEqual(after["files"], [str(self.output / f"{owner.id}.json")])
        self.assertEqual([document["production"] for document in documents], ["renamed"])
        changed = staff.by_id(owner.id)
        assert changed is not None
        self.assertEqual(changed.current_session, current)

    def test_each_pass_kind_has_its_user_facing_stage(self) -> None:
        fixture = self.production()
        for kind, label in (("impl", "Build"), ("test", "Tests"), ("review", "Review"),
                            ("fix", "Repair round 3")):
            with self.subTest(kind=kind):
                _ = self.write_run(fixture, [*self.delivery_events(), self.event(
                    "pass_started", NOW - 60, pass_instance_id="pass", pass_kind=kind,
                    fix_pass=3, pass_activity="working on delivery",
                )])
                _, unit = self.unit(fixture)
                self.assertEqual(unit["stage"], label)
                self.assertEqual(unit["state"], "running")
                self.assertEqual(unit["phase"], {
                    "kind": "plan", "id": "2", "of": 4, "title": "Current delivery",
                })

    def test_numeric_phase_names_the_assigned_ledger_ticket_with_one_read_only_connection(self) -> None:
        fixture = self.production(unit_name="names")
        self.standing_plan(fixture, phase_id="63", title="names-63")
        database = self.ledger(fixture, tickets=(
            ("names-1", "names", "merged", 1),
            ("names-2", "names", "merged", 2),
            ("names-63", "names", "assigned", 63),
        ))
        before = database.read_bytes()

        with patch("sqlite3.connect", wraps=sqlite3.connect) as connect:
            document, unit = self.unit(fixture)

        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "names-63", "title": "", "merged": 2,
        })
        self.assertEqual(unit["stage"], "Ticket names-63 running")
        self.assertIn(str(database), array_items(document["sources"]))
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connect.call_args.args, (f"file:{database}?mode=ro",))
        self.assertEqual(connect.call_args.kwargs, {"uri": True, "timeout": 0})

    def test_unit_rename_preserves_merged_count_and_active_ticket(self) -> None:
        fixture = self.production(unit_name="names")
        self.standing_plan(fixture, phase_id="124", title="Current work")
        owner = self.unit_member(fixture.name, "names")
        database = self.ledger(fixture, tickets=(
            ("123", "names", "merged", 123),
            ("124", "names", "merged", 124),
            ("125", "names", "assigned", 125),
        ))
        before = database.read_bytes(), database.stat().st_mtime_ns
        _ = staff.rename(owner.id, "renamed")

        _, unit = self.unit(fixture)

        self.assertEqual(unit["key"], "renamed")
        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "125", "title": "Current work", "merged": 2,
        })
        self.assertEqual(unit["stage"], "Ticket 125 running")
        self.assertEqual((database.read_bytes(), database.stat().st_mtime_ns), before)

    def test_renamed_unit_selects_the_open_phase_by_its_unchanged_ledger_ticket_id(self) -> None:
        fixture = self.production(unit_name="cargo-liner-style-names")
        self.standing_plan(fixture, phase_id="63", title="names-63 — Fix names")
        with fixture.plan.open("a", encoding="utf-8") as stream:
            _ = stream.write("### Phase 64 — names-64 — Later work · status: todo\n")
        _ = self.write_run(fixture, [
            self.event("phase_started", NOW - 120, phase="63", instance="ticket-63",
                       phase_title="names-63 — Fix names"),
            self.event("phase_started", NOW - 60, phase="64", instance="ticket-64",
                       phase_title="names-64 — Later work"),
        ])
        database = self.ledger(fixture, tickets=(
            ("names-63", "cargo-liner-style-names", "assigned", 63),
        ))
        before = database.read_bytes()

        _, unit = self.unit(fixture)

        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "names-63", "title": "Fix names", "merged": 0,
        })
        self.assertEqual(unit["stage"], "Ticket names-63 running")
        self.assertEqual(database.read_bytes(), before)

    def test_ledger_ahead_of_the_run_uses_the_newest_assigned_or_checkpointed_ticket(self) -> None:
        fixture = self.production(unit_name="types")
        self.standing_plan(fixture, phase_id="124", title="types-124")
        database = self.ledger(fixture)
        unit_id = self.unit_member(fixture.name, "types").id
        for state in ("assigned", "checkpointed"):
            with self.subTest(state=state):
                connection = sqlite3.connect(database)
                try:
                    _ = connection.execute("DELETE FROM tickets")
                    _ = connection.executemany("INSERT INTO tickets VALUES (?, ?, ?, ?)", [
                        ("types-125", unit_id, state, 500),
                        ("types-126", unit_id, "assigned", 300),
                        ("types-124", unit_id, "checkpointed", 400),
                        ("types-127", unit_id, "merged", 600),
                        ("types-128", unit_id, "rejected", 700),
                        ("types-129", unit_id, "open", 800),
                        ("types-extra-999", fixture_runtime_id("types-extra"), "assigned", 900),
                    ])
                    connection.commit()
                finally:
                    connection.close()

                _, unit = self.unit(fixture)

                self.assertEqual(unit["phase"], {
                    "kind": "ticket", "id": "types-125", "title": "types-124", "merged": 1,
                })
                self.assertEqual(unit["stage"], "Ticket types-125 running")

    def test_no_open_ledger_row_falls_back_to_the_ticket_word_in_the_phase_title(self) -> None:
        fixture = self.production(unit_name="names")
        self.standing_plan(fixture, phase_id="63", title="names-63")
        _ = self.ledger(fixture, tickets=(
            ("names-62", "names", "merged", 62),
            ("names-99", "names", "rejected", 99),
        ))

        _, unit = self.unit(fixture)

        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "names-63", "title": "", "merged": 1,
        })
        self.assertEqual(unit["stage"], "Ticket names-63 running")

    def test_missing_ledger_falls_back_to_the_title_ticket_and_zero_merged(self) -> None:
        fixture = self.production(unit_name="names")
        self.standing_plan(fixture, phase_id="63", title="names-63")

        _, unit = self.unit(fixture)

        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "names-63", "title": "", "merged": 0,
        })
        self.assertEqual(unit["stage"], "Ticket names-63 running")
        self.assertFalse((self.root / "style-ledgers").exists())

    def test_ticket_title_removes_only_its_leading_id_and_separator(self) -> None:
        fixture = self.production(unit_name="values")
        self.standing_plan(fixture, phase_id="8", title="values-8")
        for title, remaining in (
            ("values-8", ""),
            ("values-8 — read time", "read time"),
            ("values-8   read time", "read time"),
            ("values-8 — read values-8 time", "read values-8 time"),
        ):
            with self.subTest(title=title):
                _ = fixture.plan.write_text(
                    "> **Standing:** style_plan.py\n\n"
                    + f"### Phase 8 — {title} · status: todo\n", encoding="utf-8",
                )
                _ = self.write_run(fixture, [self.event(
                    "phase_started", NOW - 120, phase="8", instance="ticket-8", phase_title=title,
                )])

                _, unit = self.unit(fixture)

                self.assertEqual(unit["phase"], {
                    "kind": "ticket", "id": "values-8", "title": remaining, "merged": 0,
                })
                self.assertEqual(unit["stage"], "Ticket values-8 running")

    def test_non_ticket_title_word_keeps_the_phase_id_and_uses_the_ticket_stage(self) -> None:
        fixture = self.production(unit_name="names")
        self.standing_plan(fixture, phase_id="63", title="Fix names")
        for title in ("Fix names", "other-63 — read time", "names-63suffix",
                      "names-nope", "Read names-63"):
            with self.subTest(title=title):
                _ = fixture.plan.write_text(
                    "> **Standing:** style_plan.py\n\n"
                    + f"### Phase 63 — {title} · status: todo\n", encoding="utf-8",
                )
                _ = self.write_run(fixture, [self.event(
                    "phase_started", NOW - 120, phase="63", instance="ticket-63", phase_title=title,
                )])

                _, unit = self.unit(fixture)

                self.assertEqual(unit["phase"], {
                    "kind": "ticket", "id": "63", "title": title, "merged": 0,
                })
                self.assertEqual(unit["stage"], "Ticket 63 running")

    def test_locked_ledger_discards_open_ticket_and_count_then_uses_title_fallback(self) -> None:
        fixture = self.production(unit_name="types")
        self.standing_plan(fixture, phase_id="124", title="types-124")
        database = self.ledger(fixture, tickets=(
            ("types-123", "types", "merged", 123),
            ("types-125", "types", "assigned", 125),
        ))
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        _ = writer.execute("BEGIN EXCLUSIVE")

        # Count the nonblocking read; scheduler load is not a ledger wait.
        with patch("sqlite3.connect", wraps=sqlite3.connect) as connect:
            document, unit = self.unit(fixture)

        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "types-124", "title": "", "merged": 0,
        })
        self.assertEqual(unit["stage"], "Ticket types-124 running")
        self.assertIn(str(database), array_items(document["sources"]))
        connect.assert_called_once_with(f"file:{database}?mode=ro", uri=True, timeout=0)

    def test_ticket_keeps_build_test_review_and_repair_stages(self) -> None:
        fixture = self.production(unit_name="names")
        self.standing_plan(fixture, phase_id="63", title="names-63")
        for kind, label in (("impl", "Build"), ("test", "Tests"), ("review", "Review"),
                            ("fix", "Repair round 3")):
            with self.subTest(kind=kind):
                _ = self.write_run(fixture, [
                    self.event("phase_started", NOW - 120, phase="63", instance="ticket-63",
                               phase_title="names-63"),
                    self.event("pass_started", NOW - 60, phase="63", instance="ticket-63",
                               pass_instance_id="pass", pass_kind=kind, fix_pass=3),
                ])

                _, unit = self.unit(fixture)

                self.assertEqual(unit["phase"], {
                    "kind": "ticket", "id": "names-63", "title": "", "merged": 0,
                })
                self.assertEqual(unit["stage"], label)

    def test_standing_plan_counts_only_its_units_merged_tickets_with_a_read_only_connection(self) -> None:
        fixture = self.production()
        self.standing_plan(fixture)
        database = self.ledger(fixture)
        before = database.read_bytes()
        # A recorder may hold a write reservation while the pane refreshes.
        # Read-only readers must still succeed and never construct Ledger.
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        _ = writer.execute("BEGIN IMMEDIATE")
        database.chmod(0o444)
        self.addCleanup(database.chmod, 0o644)
        with patch("sqlite3.connect", wraps=sqlite3.connect) as connect:
            document, unit = self.unit(fixture)

        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "alpha-63", "title": "Fix names", "merged": 2,
        })
        self.assertIn(str(database), array_items(document["sources"]))
        self.assertEqual(database.read_bytes(), before)
        self.assertTrue(any(
            call.args and call.args[0] == f"file:{database}?mode=ro"
            and call.kwargs.get("uri") is True for call in connect.call_args_list
        ), connect.call_args_list)

    def test_exclusively_locked_ledger_gives_zero_with_one_zero_timeout_read(self) -> None:
        fixture = self.production()
        self.standing_plan(fixture)
        database = self.ledger(fixture)
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        _ = writer.execute("BEGIN EXCLUSIVE")

        # Count the nonblocking read; scheduler load is not a ledger wait.
        with patch("sqlite3.connect", wraps=sqlite3.connect) as connect:
            document, unit = self.unit(fixture)

        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "alpha-63", "title": "Fix names", "merged": 0,
        })
        self.assertIn(str(database), array_items(document["sources"]))
        connect.assert_called_once_with(f"file:{database}?mode=ro", uri=True, timeout=0)

    def test_standing_plan_without_a_ledger_gives_zero_and_does_not_create_one(self) -> None:
        fixture = self.production()
        self.standing_plan(fixture)
        ledger_root = self.root / "style-ledgers"

        document, unit = self.unit(fixture)

        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "alpha-63", "title": "Fix names", "merged": 0,
        })
        self.assertFalse(ledger_root.exists())
        self.assertFalse(any(str(path).endswith("ledger.sqlite")
                             for path in array_items(document["sources"])))

    def test_standing_plan_with_an_unreadable_ledger_keeps_zero_and_watches_the_file(self) -> None:
        fixture = self.production()
        self.standing_plan(fixture)
        database = self.ledger(fixture)
        for contents in (b"not a sqlite database", b""):
            with self.subTest(contents=contents):
                _ = database.write_bytes(contents)

                document, unit = self.unit(fixture)

                self.assertEqual(unit["phase"], {
                    "kind": "ticket", "id": "alpha-63", "title": "Fix names", "merged": 0,
                })
                self.assertIn(str(database), array_items(document["sources"]))
                self.assertEqual(database.read_bytes(), contents)

    def test_standing_plan_uses_the_home_style_ledger_when_no_override_is_set(self) -> None:
        fixture = self.production()
        self.standing_plan(fixture)
        _ = self.environment.pop("STYLE_LEDGER_STATE_DIR")
        database = self.ledger(fixture, default_path=True)

        document, unit = self.unit(fixture)

        self.assertEqual(object_fields(unit["phase"])["merged"], 2)
        self.assertIn(str(database), array_items(document["sources"]))

    def test_standing_plan_reads_a_later_merge_without_changing_the_ticket(self) -> None:
        fixture = self.production()
        self.standing_plan(fixture)
        database = self.ledger(fixture)
        _, before = self.unit(fixture)
        connection = sqlite3.connect(database)
        try:
            _ = connection.execute("UPDATE tickets SET state = 'merged' WHERE id = 'alpha-3'")
            connection.commit()
        finally:
            connection.close()

        _, after = self.unit(fixture)

        self.assertEqual(object_fields(before["phase"])["merged"], 2)
        self.assertEqual(after["phase"], {
            "kind": "ticket", "id": "alpha-63", "title": "Fix names", "merged": 3,
        })

    def test_standing_marker_is_read_from_the_runs_plan_rather_than_the_display_plan(self) -> None:
        fixture = self.production()
        self.standing_plan(fixture)
        active_plan = fixture.worktree / "ticket-plan.md"
        _ = fixture.plan.rename(active_plan)
        self.plan(fixture, all_done=True)
        _ = self.ledger(fixture)
        run = self.history / "runs/current.jsonl"
        events = [object_fields(cast(object, json.loads(line)))
                  for line in run.read_text(encoding="utf-8").splitlines()]
        events[0]["plan_doc"] = str(active_plan)
        events[0]["project_plan_doc"] = str(active_plan)
        _ = run.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

        document, unit = self.unit(fixture)

        self.assertEqual(unit["plan"], str(fixture.plan))
        self.assertEqual(unit["phase"], {
            "kind": "ticket", "id": "alpha-63", "title": "Fix names", "merged": 2,
        })
        self.assertIn(str(active_plan), array_items(document["sources"]))

    def test_standing_plan_with_no_open_ticket_keeps_a_null_phase(self) -> None:
        fixture = self.production()
        self.standing_plan(fixture)
        _ = self.ledger(fixture)
        _ = self.write_run(fixture, [
            self.event("phase_started", NOW - 120, phase="alpha-63", instance="ticket-63",
                       phase_title="Fix names"),
            self.event("phase_finished", NOW - 60, phase="alpha-63", instance="ticket-63",
                       status="completed", phase_elapsed_seconds=60),
        ])

        _, unit = self.unit(fixture)

        self.assertIsNone(unit["phase"])

    def test_standing_marker_in_prose_does_not_tag_an_ordinary_plan_as_a_ticket(self) -> None:
        fixture = self.production()
        with fixture.plan.open("a", encoding="utf-8") as stream:
            _ = stream.write("\nA standing plan uses > **Standing:** style_plan.py.\n")
        _ = self.write_run(fixture, self.delivery_events())

        _, unit = self.unit(fixture)

        self.assertEqual(unit["phase"], {
            "kind": "plan", "id": "2", "of": 4, "title": "Current delivery",
        })

    def test_dispatched_batches_name_the_repair_until_landed_in_the_current_instance(self) -> None:
        fixture = self.production()
        events = [
            *self.delivery_events(),
            self.event("finding_batch_dispatched", NOW - 400, instance="older", round=9),
            self.event("finding_batch_dispatched", NOW - 300, round=1),
            self.event("finding_batch_landed", NOW - 240, round=1),
            self.event("finding_batch_dispatched", NOW - 120, round=8),
            self.event("activity_started", NOW - 100, activity_instance_id="check",
                       activity_label="Check delivery"),
        ]
        _ = self.write_run(fixture, events)
        _, unit = self.unit(fixture)
        self.assertEqual(unit["stage"], "Repair round 2")

        events.append(self.event("pass_started", NOW - 90, pass_instance_id="review",
                                 pass_kind="review"))
        _ = self.write_run(fixture, events)
        _, unit = self.unit(fixture)
        self.assertEqual(unit["stage"], "Review")

        events += [
            self.event("pass_finished", NOW - 60, pass_instance_id="review"),
            self.event("finding_batch_landed", NOW - 30, round=8),
        ]
        _ = self.write_run(fixture, events)
        _, unit = self.unit(fixture)
        self.assertEqual(unit["stage"], "Check delivery")

    def test_run_plan_supplies_the_open_phase_while_display_keeps_the_plan_cell(self) -> None:
        fixture = self.production()
        self.plan(fixture, all_done=True)
        active_plan = fixture.worktree / "active-plan.md"
        _ = active_plan.write_text("### Phase 29 — Active work · status: todo\n", encoding="utf-8")
        for project_path in (True, False):
            with self.subTest(project_path=project_path):
                run = self.write_run(fixture, [
                    self.event("phase_started", NOW - 120, phase="29", instance="active",
                               phase_title="Active work"),
                    self.event("pass_started", NOW - 60, phase="29", instance="active",
                               pass_instance_id="build", pass_kind="impl"),
                ])
                events = [object_fields(cast(object, json.loads(line)))
                          for line in run.read_text(encoding="utf-8").splitlines()]
                events[0]["plan_doc"] = "active-plan.md"
                events[0]["project_plan_doc"] = str(active_plan) if project_path else ""
                _ = run.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

                document, unit = self.unit(fixture)

                self.assertEqual(object_fields(unit["phase"])["id"], "29")
                self.assertEqual(unit["stage"], "Build")
                self.assertEqual(unit["plan"], str(fixture.plan))
                self.assertIn(str(active_plan), array_items(document["sources"]))

    def test_neutral_run_cells_keep_the_board_activity_and_steps_in_the_open_instance(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, [
            *self.delivery_events(),
            self.event("pass_started", NOW - 300, pass_instance_id="build", pass_kind="impl",
                       pass_activity="implementing delivery"),
            self.event("pass_finished", NOW - 240, pass_instance_id="build", pass_kind="impl",
                       pass_elapsed_seconds=60, status="completed"),
            self.event("activity_started", NOW - 120, activity_instance_id="check",
                       activity_label="Final check", activity_text="checking delivery"),
            self.event("progress_reported", NOW - 110, activity="old summary"),
            self.event("progress_reported", NOW - 100, activity="delivery ready for review"),
        ])

        with (patch.object(display_cells, "open_step", wraps=display_cells.open_step) as open_step,
              patch.object(display_cells, "run_events", wraps=display_cells.run_events) as run_events):
            _, unit = self.unit(fixture)

        self.assertTrue(open_step.called)
        self.assertTrue(run_events.called)
        self.assertEqual(unit["stage"], "Final check")
        self.assertEqual(unit["summary"], "delivery ready for review")
        steps = [array_items(step) for step in array_items(unit["steps"])]
        self.assertEqual(len(steps), 2)
        self.assertEqual([step[1] for step in steps], ["implementing delivery", "checking delivery"])
        self.assertEqual([step[3] for step in steps], [60, 120])
        self.assertNotEqual(steps[0][0], steps[1][0])
        self.assertEqual(object_fields(steps[0][2])["at"], NOW - 300)

    def test_open_pass_wins_over_a_newer_step_with_live_workers(self) -> None:
        fixture = self.production()
        _ = self.session_files()
        _ = self.write_run(fixture, [
            *self.delivery_events(),
            self.event("pass_started", NOW - 300, pass_instance_id="build",
                       pass_kind="impl", pass_activity="building current delivery"),
            self.event("activity_started", NOW - 120, activity_instance_id="check",
                       activity_label="Check fixtures", activity_text="checking fixtures"),
        ])

        _, unit = self.unit(fixture)

        self.assertEqual(unit["state"], "running")
        self.assertEqual(object_fields(unit["phase"])["id"], "2")
        self.assertEqual(unit["stage"], "Build")
        self.assertTrue(any(object_fields(worker)["state"] == "running"
                            for worker in array_items(unit["workers"])))

    def test_launcher_phase_without_pass_events_names_the_running_phase(self) -> None:
        fixture = self.production()
        _ = self.session_files()
        for finished_step in (False, True):
            with self.subTest(finished_step=finished_step):
                events = self.delivery_events()
                if finished_step:
                    events += [
                        self.event("activity_started", NOW - 120, activity_instance_id="check",
                                   activity_label="Check fixtures", activity_text="checking fixtures"),
                        self.event("activity_finished", NOW - 60, activity_instance_id="check",
                                   status="completed", activity_elapsed_seconds=60),
                    ]
                _ = self.write_run(fixture, events)

                _, unit = self.unit(fixture)

                self.assertEqual(unit["state"], "running")
                self.assertEqual(object_fields(unit["phase"])["id"], "2")
                self.assertEqual(unit["stage"], "Phase 2 running")

    def test_working_workers_without_an_open_phase_stay_standing_by(self) -> None:
        fixture = self.production()
        _ = self.session_files()
        _ = self.write_run(fixture, self.delivery_events(open_phase=False))

        _, unit = self.unit(fixture)

        self.assertEqual(unit["state"], "running")
        self.assertIsNone(unit["phase"])
        self.assertEqual(unit["stage"], "Standing by")

    def test_overdue_repeated_promise_stays_distinct_from_schedule_end(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, [
            *self.delivery_events(),
            self.event("eta_stated", NOW - 3_000, eta_phase_id="2", eta_at=NOW - 1_800),
            self.event("eta_stated", NOW - 2_400, eta_phase_id="2", eta_at=NOW - 600),
            self.event("eta_stated", NOW - 1_200, eta_phase_id="2", eta_at=NOW - 600),
        ])

        _, unit = self.unit(fixture)

        eta = object_fields(unit["eta"])
        self.assertEqual(object_fields(eta["at"])["at"], NOW - 600)
        self.assertEqual(object_fields(eta["first"])["at"], NOW - 1_800)
        self.assertEqual(object_fields(eta["schedule_end"])["at"], NOW)
        self.assertEqual(eta["moved"], 1)
        self.assertEqual(eta["delta_minutes"], 20)
        trail = array_items(eta["trail"])
        self.assertEqual(len(trail), 3)
        self.assertNotIn(NOW, [object_fields(array_items(item)[0])["at"] for item in trail])
        self.environment["UNIT_NOW_EPOCH"] = str(NOW + 60)
        _, refreshed = self.unit(fixture)
        later_eta = object_fields(refreshed["eta"])
        self.assertEqual(later_eta["trail"], trail)
        self.assertEqual(later_eta["at"], eta["at"])
        self.assertEqual(object_fields(later_eta["schedule_end"])["at"], NOW + 60)

    def test_resumed_parallel_phase_keeps_its_stage_and_own_steps(self) -> None:
        fixture = self.production()
        for kind, expected_stage in (("pass", "Build"), ("activity", "Final check")):
            with self.subTest(kind=kind):
                work = (
                    self.event("pass_started", NOW - 300, pass_instance_id="resumed-build",
                               pass_kind="impl", pass_activity="building current delivery")
                    if kind == "pass" else
                    self.event("activity_started", NOW - 300, activity_instance_id="resumed-check",
                               activity_label="Final check", activity_text="checking current delivery")
                )
                _ = self.write_run(fixture, [
                    *self.delivery_events(), work,
                    self.event("phase_started", NOW - 200, phase="3", instance="three",
                               phase_title="Next delivery"),
                    self.event("pass_started", NOW - 190, phase="3", instance="three",
                               pass_instance_id="parallel-test", pass_kind="test",
                               pass_activity="testing parallel delivery"),
                    self.event("pass_finished", NOW - 110, phase="3", instance="three",
                               pass_instance_id="parallel-test", status="completed",
                               pass_elapsed_seconds=80),
                    self.event("phase_finished", NOW - 100, phase="3", instance="three",
                               status="completed", phase_elapsed_seconds=100),
                ])

                _, unit = self.unit(fixture)

                self.assertEqual(object_fields(unit["phase"])["id"], "2")
                self.assertEqual(unit["stage"], expected_stage)
                steps = [array_items(step) for step in array_items(unit["steps"])]
                self.assertEqual(len(steps), 1)
                self.assertEqual(steps[0][0], "running")
                self.assertEqual(steps[0][1], "building current delivery" if kind == "pass"
                                 else "checking current delivery")
                self.assertEqual(object_fields(steps[0][2])["at"], NOW - 300)
                self.assertEqual(steps[0][3], 300)

    def test_duration_estimate_sets_schedule_end_without_creating_a_promise(self) -> None:
        fixture = self.production()
        for started, expected_end in ((NOW - 5_400, NOW), (NOW - 180, NOW + 420)):
            with self.subTest(started=started):
                _ = self.write_run(fixture, [
                    self.event("phase_started", started - 720, phase="1", instance="one",
                               phase_title="First delivery"),
                    self.event("phase_finished", started - 120, phase="1", instance="one",
                               status="completed", phase_elapsed_seconds=600),
                    self.event("phase_started", started, phase_title="Current delivery"),
                ])

                _, unit = self.unit(fixture)

                eta = object_fields(unit["eta"])
                self.assertIsNone(eta["at"])
                self.assertIsNone(eta["first"])
                self.assertEqual(eta["trail"], [])
                self.assertEqual(object_fields(eta["schedule_end"])["at"], expected_end)
                next_phase = object_fields(array_items(unit["phases"])[2])
                self.assertEqual(object_fields(next_phase["start"])["at"], expected_end + 120)

    def test_run_state_does_not_read_unrelated_history_payloads(self) -> None:
        fixture = self.production()
        unrelated = self.history / "runs/unrelated.jsonl"
        _ = unrelated.write_text(json.dumps({
            "event_type": "run_started", "working_dir": str(self.root / "unrelated"),
            "plan_doc": "other.md", "run_started_at": NOW - 100,
        }) + "\n" + json.dumps({"event_type": "run_finished"}) + "\n", encoding="utf-8")
        read_text = Path.read_text

        def read_unit_file(path: Path, encoding: str | None = None,
                           errors: str | None = None) -> str:
            self.assertNotEqual(path, unrelated, "unrelated run payload read for unit state")
            return read_text(path, encoding=encoding, errors=errors)

        for finished, expected in ((False, "running"), (True, "run-finished")):
            with self.subTest(finished=finished):
                events = self.delivery_events()
                if finished:
                    events.append(self.event("run_finished", NOW - 10, status="completed"))
                _ = self.write_run(fixture, events)
                # Discovery may inspect run headers; unrelated full payloads
                # must not be read again to derive this unit's state.
                with patch.object(Path, "read_text", read_unit_file):
                    _, unit = self.unit(fixture)
                self.assertEqual(unit["state"], expected)

    def test_running_fix_is_not_counted_as_a_finished_fix(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, [
            *self.delivery_events(),
            self.event("pass_started", NOW - 900, pass_instance_id="fix-one", pass_kind="fix"),
            self.event("pass_finished", NOW - 600, pass_instance_id="fix-one", pass_kind="fix",
                       status="completed", pass_elapsed_seconds=300),
            self.event("pass_started", NOW - 300, pass_instance_id="fix-two", pass_kind="fix"),
            self.event("finding_opened", NOW - 250, severity="minor"),
            self.event("eta_stated", NOW - 100, eta_phase_id="2", eta_at=NOW + 1_800),
        ])

        _, unit = self.unit(fixture)

        eta = object_fields(unit["eta"])
        self.assertEqual((eta["fix_done"], eta["fix_running"]), (1, 1))
        row = object_fields(array_items(unit["phases"])[1])
        self.assertEqual((row["fix_done"], row["fix_running"]), (1, 1))
        self.assertEqual(row["findings"], {"blocker": 0, "minor": 1, "nit": 0})


    def test_no_run_is_standing_by_with_unknown_finish(self) -> None:
        fixture = self.production()
        _, unit = self.unit(fixture)
        self.assertEqual(unit["state"], "standing-by")
        self.assertEqual(unit["stage"], "Standing by")
        self.assertIsNone(unit["phase"])
        self.assertIsNone(unit["eta"])
        self.assertEqual(unit["finish"], {"kind": "unknown"})


    def test_finished_plan_exposes_the_actual_finish_and_done_phase_history(self) -> None:
        fixture = self.production()
        self.plan(fixture, all_done=True)
        events = self.delivery_events()
        events += [
            self.event("eta_stated", NOW - 4_800, eta_phase_id="2", eta_at=NOW - 4_200),
            self.event("phase_finished", NOW - 4_080, status="completed", phase_elapsed_seconds=1_320),
            self.event("phase_started", NOW - 3_000, phase="3", instance="three", phase_title="Next delivery"),
            self.event("phase_finished", NOW - 2_400, phase="3", instance="three", status="completed", phase_elapsed_seconds=600),
            self.event("phase_started", NOW - 1_800, phase="4", instance="four", phase_title="Last delivery"),
            self.event("phase_finished", NOW - 1_200, phase="4", instance="four", status="completed", phase_elapsed_seconds=600),
            self.event("run_finished", NOW - 1_000, phase="", instance="", status="completed"),
        ]
        _ = self.write_run(fixture, events)

        _, unit = self.unit(fixture)

        self.assertEqual(unit["state"], "run-finished")
        self.assertEqual(unit["stage"], "Run done")
        self.assertIsNone(unit["phase"])
        finish = object_fields(unit["finish"])
        self.assertEqual(finish["kind"], "finished")
        self.assertEqual(object_fields(finish["at"])["at"], NOW - 1_200)
        done = object_fields(array_items(unit["phases"])[1])
        self.assertEqual(done["took"], 1_320)
        eta = object_fields(done["eta"])
        self.assertEqual(object_fields(eta["first"])["at"], NOW - 4_200)
        self.assertEqual(object_fields(eta["finished"])["at"], NOW - 4_080)
        self.assertEqual(eta["delta_minutes"], 2)

    def test_workers_read_models_and_only_their_latest_board_message(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        _ = self.session_files()

        _, unit = self.unit(fixture)

        workers = {str(worker["slot"]): worker for worker in
                   (object_fields(row) for row in array_items(unit["workers"]))}
        self.assertEqual(set(workers), {"1", "test"})
        self.assertEqual(workers["1"]["role"], "impl")
        self.assertEqual(workers["1"]["state"], "running")
        self.assertEqual(workers["1"]["model"], "codex/fixture-turn-model:high")
        doing = str(workers["1"]["doing"])
        self.assertTrue(doing.startswith("building "), doing)
        self.assertLessEqual(len(doing), 80)
        self.assertNotIn("obsolete", doing)
        self.assertEqual(workers["test"]["doing"], "checking the delivery")

    def test_workers_shorten_mesh_names_using_the_worktree_prefix(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        directory = self.session_files()
        _ = (directory / "impl_worker_test.json").write_text(json.dumps({
            "name": "claude-build-followups-style-test", "slot": "test",
            "family": "codex", "model": "fixture-model",
        }), encoding="utf-8")
        _ = (directory / "mesh_roster.json").write_text(json.dumps({
            "claude-build-followups-style-impl": {
                "slot": "claude-build-followups-style-impl", "role": "", "status": "running",
            },
            "claude-build-followups-style-test": {"role": "test", "status": "paused"},
        }), encoding="utf-8")
        _ = (directory / "board.log").write_text(
            "2026-10-16T17:02:00Z [claude-build-followups-style-impl] status: building the current phase\n",
            encoding="utf-8",
        )

        _, unit = self.unit(fixture)

        rows = [object_fields(row) for row in array_items(unit["workers"])]
        self.assertEqual({str(row["slot"]) for row in rows}, {"impl", "test"})
        worker = next(row for row in rows if row["slot"] == "impl")
        self.assertEqual(worker["state"], "running")
        self.assertEqual(worker["doing"], "building the current phase")

    def test_worker_metadata_selects_state_while_latest_slot_activity_is_retained(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        directory = self.session_files()
        _ = (directory / "impl_worker_impl.json").write_text(json.dumps({
            "name": "claude-build-followups-style-impl", "slot": "impl",
            "family": "codex", "model": "fixture-model",
        }), encoding="utf-8")
        aliases = [
            ("claude-build-followups-style-impl", {"role": "impl", "status": "paused"}),
            ("impl", {"slot": "impl", "role": "fix", "status": "running"}),
        ]
        _ = (directory / "board.log").write_text(
            "2026-10-16T17:01:00Z [claude-build-followups-style-impl] status: old assignment\n"
            + "2026-10-16T17:02:00Z [impl] status: repairing the latest phase\n",
            encoding="utf-8",
        )

        for entries in (aliases, list(reversed(aliases))):
            with self.subTest(roster_order=[name for name, _ in entries]):
                _ = (directory / "mesh_roster.json").write_text(
                    json.dumps(dict(entries)), encoding="utf-8",
                )

                _, unit = self.unit(fixture)

                rows = [object_fields(row) for row in array_items(unit["workers"])]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["slot"], "impl")
                self.assertEqual(rows[0]["state"], "paused")
                self.assertEqual(rows[0]["role"], "impl")
                self.assertEqual(rows[0]["doing"], "repairing the latest phase")

    def test_current_worker_metadata_overrides_obsolete_alias_revisions_and_roster_order(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        directory = self.session_files()
        current = "claude-build-followups-style-fix"
        obsolete = "claude-build-followups-style-impl"
        _ = (directory / "impl_worker_impl.json").write_text(json.dumps({
            "name": current, "slot": "impl", "family": "codex", "model": "fixture-model",
        }), encoding="utf-8")
        aliases = [
            (current, {"role": "fix", "status": "done", "revision": 438}),
            (obsolete, {"role": "review", "status": "paused", "revision": 547}),
        ]
        _ = (directory / "board.log").write_text(
            f"2026-10-16T17:01:00Z [{current}] status: repair completed\n"
            + f"2026-10-16T17:02:00Z [{obsolete}] status: latest slot activity\n",
            encoding="utf-8",
        )
        for entries in (aliases, list(reversed(aliases))):
            with self.subTest(roster_order=[name for name, _ in entries]):
                _ = (directory / "mesh_roster.json").write_text(json.dumps(dict(entries)), encoding="utf-8")

                rows = board_json.workers(directory, set(), "claude-build-followups-style")

                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["slot"], "impl")
                self.assertEqual(rows[0]["state"], "idle")
                self.assertEqual(rows[0]["role"], "fix")
                self.assertEqual(rows[0]["doing"], "latest slot activity")

    def test_worker_aliases_without_metadata_use_the_last_roster_entry(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        directory = self.session_files()
        aliases = [
            ("old-name", {"slot": "impl", "role": "review", "status": "paused", "revision": 547}),
            ("new-name", {"slot": "impl", "role": "fix", "status": "running", "revision": 1}),
        ]
        _ = (directory / "board.log").write_text(
            "2026-10-16T17:01:00Z [new-name] status: initial repair\n"
            + "2026-10-16T17:02:00Z [old-name] status: newest activity on this slot\n",
            encoding="utf-8",
        )
        for entries in (aliases, list(reversed(aliases))):
            with self.subTest(roster_order=[name for name, _ in entries]):
                _ = (directory / "mesh_roster.json").write_text(json.dumps(dict(entries)), encoding="utf-8")

                rows = board_json.workers(directory, set())

                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["role"], entries[-1][1]["role"])
                self.assertEqual(rows[0]["state"], entries[-1][1]["status"])
                self.assertEqual(rows[0]["doing"], "newest activity on this slot")

    def test_worker_states_distinguish_a_finished_turn_from_a_closed_thread(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        directory = self.session_files()
        statuses = {"running": "running", "paused": "paused", "idle": "idle",
                    "done": "idle", "ended": "ended", "connecting": "connecting"}
        _ = (directory / "mesh_roster.json").write_text(json.dumps({
            f"worker-{status}": {"slot": status, "role": "test", "status": status,
                                 "thread_id": f"thread-{status}", "turn_id": ""}
            for status in statuses
        }), encoding="utf-8")

        _, unit = self.unit(fixture)

        self.assertEqual({str(worker["slot"]): worker["state"] for worker in
                          (object_fields(row) for row in array_items(unit["workers"]))},
                         statuses)

    def test_worker_activity_removes_launcher_and_report_paths_from_latest_message(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        directory = self.session_files()
        for message, expected in (
            ("launcher: adversary review finished; summary at /tmp/review.md",
             "adversary review finished"),
            ("launcher: adversary review finished; findings in /tmp/findings.json",
             "adversary review finished"),
            ("adversary review finished; summary at /tmp/summary; findings in /tmp/findings",
             "adversary review finished"),
            ("launcher: tests passed; checking the next case", "tests passed; checking the next case"),
        ):
            with self.subTest(message=message):
                _ = (directory / "board.log").write_text(
                    "2026-10-16T17:00:00Z [alpha-impl] status: obsolete activity\n"
                    + f"2026-10-16T17:01:00Z [alpha-impl] status: {message}\n"
                    + "2026-10-16T17:02:00Z [alpha-test] status: another worker\n",
                    encoding="utf-8",
                )
                _, unit = self.unit(fixture)
                worker = next(object_fields(row) for row in array_items(unit["workers"])
                              if object_fields(row)["slot"] == "1")
                self.assertEqual(worker["doing"], expected)

    def test_worker_activity_trims_whitespace_after_removing_report_paths(self) -> None:
        directory = self.session_files()
        for message in (
            "launcher:  review finished  ; summary at /tmp/summary.md",
            "launcher:  review finished  ; findings in /tmp/findings.json",
            " review finished ",
        ):
            with self.subTest(message=message):
                _ = (directory / "board.log").write_text(
                    f"2026-10-16T17:01:00Z [alpha-impl] status: {message}\n",
                    encoding="utf-8",
                )

                workers = board_json.workers(directory, set())

                worker = next(row for row in workers if row["slot"] == "1")
                self.assertEqual(worker["doing"], "review finished")

    def test_run_events_supply_phase_records_when_the_plan_has_no_phase_headings(self) -> None:
        fixture = self.production()
        _ = fixture.plan.write_text("# As built\n\nDelivered feature overview.\n", encoding="utf-8")
        # A different run must not contribute phase rows, findings or promises.
        _ = self.write_run(fixture, [
            self.event("phase_started", NOW - 19_000, phase="99", instance="unrelated",
                       phase_title="Old run"),
            self.event("phase_finished", NOW - 18_000, phase="99", instance="unrelated",
                       phase_title="Old run", phase_elapsed_seconds=1_000),
        ], name="older", started=NOW - 20_000)
        _ = self.write_run(fixture, [
            self.event("phase_started", NOW - 6_000, phase="7", instance="first",
                       phase_title="Original delivery"),
            self.event("pass_started", NOW - 5_900, phase="7", instance="first",
                       pass_instance_id="repair-first", pass_kind="fix"),
            self.event("pass_finished", NOW - 5_700, phase="7", instance="first",
                       pass_instance_id="repair-first", pass_kind="fix", status="completed"),
            self.event("finding_opened", NOW - 5_650, phase="7", instance="first", severity="blocker"),
            self.event("eta_stated", NOW - 5_600, phase="7", instance="first",
                       eta_phase_id="7", eta_at=NOW - 5_000),
            self.event("eta_stated", NOW - 5_500, phase="7", instance="first",
                       eta_phase_id="7", eta_at=NOW - 4_900),
            self.event("phase_finished", NOW - 4_800, phase="7", instance="first",
                       phase_title="Original delivery", phase_elapsed_seconds=1_200),
            # Repeated ids are separate instances, not a single combined row.
            self.event("phase_started", NOW - 4_000, phase="7", instance="retry",
                       phase_title="Retried delivery"),
            self.event("finding_opened", NOW - 3_900, phase="7", instance="retry", severity="minor"),
            self.event("finding_opened", NOW - 3_800, phase="7", instance="retry", severity="nit"),
            self.event("eta_stated", NOW - 3_700, phase="7", instance="retry",
                       eta_phase_id="7", eta_at=NOW - 3_300),
            self.event("phase_finished", NOW - 3_000, phase="7", instance="retry",
                       phase_title="Retried delivery", phase_elapsed_seconds=1_000),
            self.event("phase_started", NOW - 1_000, phase="8", instance="open",
                       phase_title="Follow-up delivery"),
            self.event("pass_started", NOW - 900, phase="8", instance="open",
                       pass_instance_id="repair-open", pass_kind="fix"),
            self.event("finding_opened", NOW - 800, phase="8", instance="open", severity="minor"),
            # eta_phase_id identifies the target, even when the event's phase differs.
            self.event("eta_stated", NOW - 700, phase="7", instance="open",
                       eta_phase_id="8", eta_at=NOW + 600),
        ])

        _, unit = self.unit(fixture)

        rows = [object_fields(row) for row in array_items(unit["phases"])]
        self.assertEqual([(row["id"], row["title"], row["status"]) for row in rows],
                         [("7", "Original delivery", "done"),
                          ("7", "Retried delivery", "done"),
                          ("8", "Follow-up delivery", "open")])
        self.assertEqual([row["took"] for row in rows[:2]], [1_200, 1_000])
        self.assertEqual(object_fields(unit["phase"]),
                         {"kind": "plan", "id": "8", "of": 3, "title": "Follow-up delivery"})
        self.assertEqual([(row["fix_done"], row["fix_running"], row["findings"]) for row in rows], [
            (1, 0, {"blocker": 1, "minor": 0, "nit": 0}),
            (0, 0, {"blocker": 0, "minor": 1, "nit": 1}),
            (0, 1, {"blocker": 0, "minor": 1, "nit": 0}),
        ])
        first_eta = object_fields(rows[0]["eta"])
        self.assertEqual(first_eta, {
            "first": {"at": NOW - 5_000, "text": "16:36"},
            "trail": [[{"at": NOW - 5_000, "text": "16:36"},
                       {"at": NOW - 5_600, "text": "16:26"}],
                      [{"at": NOW - 4_900, "text": "16:38"},
                       {"at": NOW - 5_500, "text": "16:28"}]],
            "finished": {"at": NOW - 4_800, "text": "16:40"},
            "delta_minutes": 100 / 60,
        })
        retry_eta = object_fields(rows[1]["eta"])
        self.assertEqual(object_fields(retry_eta["first"])["at"], NOW - 3_300)
        self.assertEqual(len(array_items(retry_eta["trail"])), 1)
        self.assertEqual(object_fields(retry_eta["finished"])["at"], NOW - 3_000)
        self.assertEqual(retry_eta["delta_minutes"], 5)
        open_eta = object_fields(rows[2]["eta"])
        self.assertEqual(object_fields(open_eta["first"])["at"], NOW + 600)
        self.assertIsNone(open_eta["finished"])
        self.assertIsNone(open_eta["delta_minutes"])
        for row in rows:
            self.assertEqual(set(row), {"id", "title", "status", "took", "fix_done", "fix_running",
                                        "findings", "eta", "start", "finish", "gate"})
        with (self.history / "runs/current.jsonl").open("a", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(self.event(
                "run_finished", NOW - 10, phase="", instance="", status="completed",
            )) + "\n")
        _, finished_unit = self.unit(fixture)
        self.assertEqual(finished_unit["state"], "run-finished")
        self.assertEqual(finished_unit["phases"], rows)

    def test_sources_include_history_workers_and_discovery_dirs_but_no_missing_paths(self) -> None:
        fixture = self.production()
        old = self.write_run(fixture, [], name="older", started=NOW - 10_000)
        current = self.write_run(fixture, self.delivery_events())
        directory = self.session_files()
        unrelated = self.history / "runs/unrelated.jsonl"
        _ = unrelated.write_text(json.dumps({
            "event_type": "run_started", "working_dir": str(self.root / "unrelated"),
            "plan_doc": "unrelated.md", "run_started_at": NOW - 20_000,
        }) + "\n", encoding="utf-8")

        document, _ = self.unit(fixture)

        sources = {Path(str(path)) for path in array_items(document["sources"])}
        expected = {old, current, fixture.plan, fixture.document, self.history / "runs", directory,
                    directory / "mesh_roster.json", directory / "impl_worker_1.json", directory / "board.log"}
        self.assertTrue(expected <= sources, expected - sources)
        self.assertNotIn(unrelated, sources)
        self.assertNotIn(directory / "impl_worker_test.json", sources)
        self.assertTrue(all(path.is_absolute() and path.exists() for path in sources))

    def test_sources_allow_discovery_of_a_run_and_later_session_files(self) -> None:
        fixture = self.production()
        document, _ = self.unit(fixture)
        sources = {Path(str(path)) for path in array_items(document["sources"])}
        self.assertIn(self.history / "runs", sources)
        # Pin an old mtime rather than sleeping for filesystem resolution.
        os.utime(self.history / "runs", (1, 1))
        before = (self.history / "runs").stat().st_mtime_ns
        _ = self.write_run(fixture, self.delivery_events())
        directory = self.run_root / "current"
        directory.mkdir()
        self.assertGreater((self.history / "runs").stat().st_mtime_ns, before)
        document, unit = self.unit(fixture)
        self.assertEqual(unit["state"], "running")
        self.assertIn(str(directory), array_items(document["sources"]))
        os.utime(directory, (1, 1))
        before = directory.stat().st_mtime_ns
        _ = self.session_files()
        self.assertGreater(directory.stat().st_mtime_ns, before)
        _, unit = self.unit(fixture)
        self.assertEqual(len(array_items(unit["workers"])), 2)

    def test_session_lookup_uses_first_existing_root_and_supports_the_former_root(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        former = self.session_files(former=True)
        _, unit = self.unit(fixture)
        self.assertEqual(len(array_items(unit["workers"])), 2)
        current = self.run_root / "current"
        current.mkdir()
        _ = (current / "mesh_roster.json").write_text("{}", encoding="utf-8")
        document, unit = self.unit(fixture)
        self.assertEqual(unit["workers"], [])
        self.assertIn(str(current), array_items(document["sources"]))
        self.assertNotIn(str(former / "board.log"), array_items(document["sources"]))

    def test_times_use_the_users_zone_and_distinguish_other_dates(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, [*self.delivery_events(), self.event(
            "eta_stated", NOW - 60, eta_phase_id="2", eta_at=NOW + 86_400,
        )])
        self.environment["USER_ZONE"] = "America/New_York"

        document, unit = self.unit(fixture)

        self.assertEqual(document["zone"], "America/New_York")
        self.assertEqual(document["utc_offset_minutes"], -240)
        self.assertEqual(document["as_of"], {"at": NOW, "text": "14:00"})
        eta = object_fields(unit["eta"])
        self.assertEqual(eta["at"], {"at": NOW + 86_400, "text": "Sat 17 Oct 14:00"})

    def test_atomic_replace_receives_complete_json_and_failure_preserves_the_old_file(self) -> None:
        fixture = self.production()
        _ = self.write_run(fixture, self.delivery_events())
        _ = self.unit(fixture)
        target = self.cache_path("show")
        previous = target.read_bytes()
        captured: list[dict[str, object]] = []
        real_replace = os.replace

        def observe_replace(source: str | Path, destination: str | Path) -> None:
            self.assertEqual(Path(source).parent, target.parent)
            self.assertEqual(Path(destination), target)
            self.assertNotEqual(Path(source), target)
            self.assertEqual(target.read_bytes(), previous)
            captured.append(object_fields(cast(object, json.loads(Path(source).read_text()))))
            real_replace(source, destination)

        with patch("os.replace", side_effect=observe_replace):
            _ = self.unit(fixture)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0], object_fields(cast(object, json.loads(target.read_text()))))
        previous = target.read_bytes()
        with patch("os.replace", side_effect=OSError("fixture cannot replace destination")):
            with self.assertRaisesRegex(OSError, "fixture cannot replace destination"):
                board_json.write_board(target, captured[0])
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(list(self.output.glob("*.json")), [target])

    def test_all_emitted_object_keys_are_declared_in_the_typescript_contract(self) -> None:
        fixture = self.production()
        _ = self.session_files()
        samples: list[dict[str, object]] = []
        self.plan(fixture)
        _ = self.write_run(fixture, [*self.delivery_events(), self.event(
            "eta_stated", NOW - 60, eta_phase_id="2", eta_at=NOW + 1_800,
        )])
        document, _ = self.unit(fixture)
        samples.append(document)
        self.standing_plan(fixture)
        _ = self.ledger(fixture)
        document, _ = self.unit(fixture)
        samples.append(document)
        declared = set(re.findall(r"\b([A-Za-z_]\w*)\??\s*:", CONTRACT.read_text(encoding="utf-8")))

        def check_fields(value: object, location: str) -> None:
            if isinstance(value, dict):
                for key, child in cast(dict[str, object], value).items():
                    self.assertIn(key, declared, f"{location}.{key} missing from {CONTRACT}")
                    check_fields(child, f"{location}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(cast(list[object], value)):
                    check_fields(child, f"{location}[{index}]")

        for sample in samples:
            check_fields(sample, "board")


class PaneBoardTests(StaffTestCase):
    def pane_run(self) -> tuple[Path, staff.StaffRow, staff.StaffRow]:
        parent = fixture_member("unit", name="pane-unit", parent=fixture_member("showrunner", name="pane-runner").id, worktree=staff.PresentWorktree(remote.local_machine(), self.root))
        worker = fixture_worker("reader", parent.id)
        run = self.root / "run"
        run.mkdir()
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: parent.id}))
        write_text_atomic(run / "worker_transport", "herdr\n")
        return run, parent, worker

    def test_pane_worker_sources_include_posts_and_launcher_writes(self) -> None:
        run, parent, worker = self.pane_run()
        write_text_atomic(run / "board.log", "reader status: reading\n")
        record = staff.snapshot()
        sources: set[Path] = set()
        with patch.object(unit_runs, "held", return_value=[str(run)]), patch.object(
                worker_panes, "observe_parents", return_value={parent.id: [worker_panes.ClosedWorker(worker)]}):
            rows = board_json.workers(run, sources, record=record)
        self.assertEqual(rows[0]["state"], "closed")
        self.assertIn(run, sources)
        self.assertIn(run / "board.log", sources)

    def test_named_read_states_and_supplied_rows_use_the_selected_record(self) -> None:
        run, parent, worker = self.pane_run()
        state = worker_panes.OpenWorker(worker, "pane-live", "terminal", herdr.AgentStatus.WORKING)
        with patch.object(unit_runs, "held", return_value=[str(run)]), patch.object(
                worker_panes, "observe_parents", return_value={parent.id: [state]}), patch.object(
                staff, "snapshot", wraps=staff.snapshot) as snapshot:
            rows = board_json.workers(run, set(), record=unit_runs.RunStaffAtUse.LOCAL,
                                      pane_rows=board_json.ReadPaneRows.CURRENT)
        self.assertEqual((rows[0]["state"], rows[0]["doing"]), ("working", "pane-live"))
        self.assertEqual(snapshot.call_count, 1)
        record = staff.snapshot()
        with patch.object(staff, "snapshot", side_effect=AssertionError("extra staff read")), patch.object(
                worker_panes, "observe_parents", side_effect=AssertionError("extra pane read")):
            supplied = board_json.workers(run, set(), record=record, pane_rows={parent.id: rows})
        self.assertEqual(supplied, rows)

    def test_unreadable_pin_names_the_cause_and_keeps_a_problem_worker(self) -> None:
        run = self.pane_run()[0]
        pin = run / "worker_transport"
        pin.unlink()
        pin.mkdir()
        error = io.StringIO()
        with patch.object(unit_runs, "held", return_value=[str(run)]), redirect_stderr(error):
            rows = board_json.workers(run, set())
        self.assertEqual(rows[0]["state"], "incomplete")
        self.assertIn(str(pin), str(rows[0]["doing"]))
        self.assertIn(str(pin), error.getvalue())

    def test_five_pane_states_keep_the_pane_and_the_reason(self) -> None:
        run, parent, worker = self.pane_run()
        cases: tuple[tuple[worker_panes.WorkerPane, str, str], ...] = (
            (worker_panes.OpenWorker(worker, "pane-live", "terminal", herdr.AgentStatus.WORKING), "working", "pane-live"),
            (worker_panes.StoppedWorker(worker, "pane-stop", "terminal"), "stopped", "pane-stop"),
            (worker_panes.ClosedWorker(worker), "closed", ""),
            (worker_panes.ProcessUnknownWorker(worker, "pane-unknown", "terminal", "process unreadable"), "unknown", "process unreadable"),
            (worker_panes.UncertainWorker(worker, "pane read incomplete"), "incomplete", "pane read incomplete"),
        )
        for state, expected, detail in cases:
            with self.subTest(state=expected), patch.object(unit_runs, "held", return_value=[str(run)]), patch.object(
                    worker_panes, "observe_parents", return_value={parent.id: [state]}), patch.object(
                    staff, "snapshot", wraps=staff.snapshot) as snapshot:
                rows = board_json.workers(run, set())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["slot"], "reader")
            self.assertEqual(rows[0]["state"], expected)
            if detail:
                self.assertIn(detail, str(rows[0]["doing"]))
            self.assertEqual(snapshot.call_count, 1)

    def test_many_parents_use_one_pane_read_without_a_pin_write(self) -> None:
        from scripts.herdr.test_worker_panes import PaneWorld

        runner = fixture_member("showrunner", name="runner")
        world = PaneWorld()
        for index in range(2):
            parent = fixture_member("unit", name=f"unit-{index}", parent=runner.id,
                                    worktree=staff.PresentWorktree(remote.local_machine(), self.root / str(index)))
            worker = fixture_worker("reader", parent.id)
            world.add(worker, f"w1:p{index}")
            world.locations[f"w1:p{index}"]["agent_status"] = herdr.AgentStatus.WORKING
        record = staff.snapshot()
        with patch.object(unit_runs, "parent_transport", return_value="herdr"), patch.object(
                worker_panes, "_client", return_value=world), patch.object(
                herdr, "native_process", side_effect=world.native_process), patch.object(world, "panes", wraps=world.panes) as panes:
            rows = board_json.pane_worker_rows(record)
        self.assertEqual(len(rows), 2)
        self.assertEqual({str(row["doing"]) for group in rows.values() for row in group}, {"w1:p0", "w1:p1"})
        self.assertEqual({str(row["state"]) for group in rows.values() for row in group}, {"working"})
        self.assertEqual(panes.call_count, 1)
        self.assertEqual(list(self.root.rglob("worker_transport")), [])

    def test_mesh_run_does_not_observe_panes_or_write_a_pin(self) -> None:
        write_text_atomic(self.staff_file, '{"schema": 2, "rows": []}')
        run = self.root / "run"
        run.mkdir()
        write_text_atomic(run / "mesh_roster.json", json.dumps({"reader": {"status": "paused", "role": "test"}}))
        with patch.object(worker_panes, "observe_parents", side_effect=AssertionError("mesh pane read")):
            rows = board_json.workers(run, set())
        self.assertEqual([(row["slot"], row["state"]) for row in rows], [("reader", "paused")])
        self.assertFalse((run / "worker_transport").exists())

    def test_mesh_worker_rows_keep_full_names_beside_short_slots(self) -> None:
        write_text_atomic(self.staff_file, '{"schema": 2, "rows": []}')
        run = self.root / "named-run"
        run.mkdir()
        name = "unit-full-review"
        write_text_atomic(run / "worker_transport", "mesh\n")
        write_text_atomic(run / "mesh_roster.json", json.dumps({
            name: {"slot": "review", "status": "running", "task": "direct.review"}}))
        rows = board_json.workers(run, set())
        self.assertEqual([(row["name"], row["slot"], row["role"]) for row in rows], [(name, "review", "review")])

    def test_unreachable_owner_gives_a_problem_worker(self) -> None:
        run = self.pane_run()[0]
        error = io.StringIO()
        with self.machine("mac"), patch.dict(os.environ, {"STAFF_TEST_SSH_FAIL": "1"}), redirect_stderr(error):
            rows = board_json.workers(run, set())
        self.assertIn("natedev not reached", error.getvalue())
        self.assertEqual(rows[0]["state"], "incomplete")
        self.assertIn("natedev not reached", str(rows[0]["doing"]))

    def test_malformed_staff_gives_a_problem_worker(self) -> None:
        run = self.root / "run"
        write_text_atomic(self.staff_file, "not json")
        error = io.StringIO()
        with redirect_stderr(error):
            rows = board_json.workers(run, set())
        self.assertIn("staff", error.getvalue())
        self.assertEqual(rows[0]["state"], "incomplete")

    def test_missing_run_member_names_the_reference(self) -> None:
        run = self.pane_run()[0]
        missing = fixture_runtime_id("missing-board-parent")
        write_text_atomic(run / unit_runs.STATE_FILE, json.dumps({unit_runs.OWNER_FIELD: missing}))
        error = io.StringIO()
        with patch.object(unit_runs, "held", return_value=[str(run)]), redirect_stderr(error):
            rows = board_json.workers(run, set())
        self.assertIn(missing, error.getvalue())
        self.assertEqual(rows[0]["state"], "incomplete")
        self.assertIn(missing, str(rows[0]["doing"]))


if __name__ == "__main__":
    _ = unittest.main()
