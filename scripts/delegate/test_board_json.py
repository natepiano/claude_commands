"""The board contract is generated from disposable production and run records."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.delegate import board_json
from scripts.lib import unit_runs
from scripts.production import fake_showrunner
from scripts.production.cli_fixture import invoke_cli


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
    slug: str
    checkout: Path
    worktree: Path
    plan: Path
    document: Path


class BoardJsonTests(unittest.TestCase):
    root: Path = Path()
    history: Path = Path()
    run_root: Path = Path()
    former_root: Path = Path()
    notifier: Path = Path()
    sessions: Path = Path()
    session_script: Path = Path()
    output: Path = Path()
    environment: dict[str, str] = {}
    production_count: int = 0

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.history = self.root / "history"
        (self.history / "runs").mkdir(parents=True)
        self.run_root = self.root / "unit"
        self.former_root = self.root / "delegate"
        self.run_root.mkdir()
        self.former_root.mkdir()
        self.notifier = self.root / "notifier"
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
            "PLAN_DELEGATE_HISTORY_DIR": str(self.history),
            "PLAN_DELEGATE_NOW_EPOCH": str(NOW),
            "PLAN_DELEGATE_SYNC_WRITES": "0",
            "PHASE_TABLE_VAULT": str(self.root / "vault"),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "NOTIFIER_SESSIONS_DIR": str(self.sessions),
            "SHOWRUNNERS_SESSIONS": str(self.session_script),
            "UNIT_BOARD_DIR": str(self.output),
            "XDG_STATE_HOME": str(self.root / "state"),
        }

    def production(self, slug: str = "show", *, running: bool = True) -> ProductionFixture:
        checkout = self.root / f"{slug}-checkout"
        worktree = self.root / f"{slug}-unit"
        checkout.mkdir()
        worktree.mkdir()
        # live_unit_table retains a linked worktree without querying session marks.
        _ = (worktree / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
        plan = worktree / "plan.md"
        document = checkout / f"{slug}-production.md"
        _ = document.write_text("\n".join((
            f"# Production — {slug}", "", "## Production Context", "",
            "- **Merge branch:** main",
            f"- **Showrunner checkout:** {checkout}",
            "- **Log:** show.log", "- **User zone:** UTC", "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Owns |",
            "| --- | --- | --- | --- | --- |",
            f"| alpha-unit | {plan} | {worktree} | branch | fixture.py |", "",
        )), encoding="utf-8")
        fixture = ProductionFixture(slug, checkout, worktree, plan, document)
        self.plan(fixture)
        _ = fake_showrunner.write_timer(self.notifier, slug, f"{slug}-id", "UTC", document)
        if running:
            process_ids = (os.getpid(), os.getppid(), 1)
            held = fake_showrunner.write_session(
                self.sessions, f"{slug}-showrunner", f"{slug}-id",
                pid=process_ids[self.production_count],
            )
            self.addCleanup(held.close)
        self.production_count += 1
        return fixture

    def plan(self, fixture: ProductionFixture, *, gate: str = "", all_done: bool = False) -> None:
        status = "done" if all_done else "todo"
        _ = fixture.plan.write_text("\n".join((
            f"> **Production: {fixture.slug}** — unit `alpha-unit`; production doc `{fixture.document}`",
            "", "### Phase 1 — First delivery  · status: done", "",
            f"### Phase 2 — Current delivery  · status: {status}", "",
            f"### Phase 3 — Next delivery  · status: {status}", "",
            *(["**Blocked by:** G2", ""] if gate else []),
            f"### Phase 4 — Last delivery  · status: {status}", "",
        )), encoding="utf-8")
        text = fixture.document.read_text(encoding="utf-8").split("\n## Gates", 1)[0]
        _ = fixture.document.write_text(text + "\n" + "\n".join((
            "## Gates", "", "| Gate | Waiting | Waits on | Clears when |",
            "| --- | --- | --- | --- |",
            f"| G2 | alpha phase 3 | renderer phase 9 | {gate} |", "",
        )), encoding="utf-8")

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
        first: dict[str, object] = {
            "event_type": "run_started", "working_dir": str(fixture.worktree),
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
        _ = (directory / "mesh_roster.json").write_text(json.dumps({
            "alpha-impl": {"slot": "1", "role": "impl", "status": "running",
                           "thread_id": "thread-one", "turn_id": "turn-one"},
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

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        with ExitStack() as scope:
            _ = scope.enter_context(patch.object(
                unit_runs, "run_roots", return_value=(self.run_root, self.former_root),
            ))
            # Both import spellings occur in production scripts; rebind their
            # import-time path constants, without replacing the registry reader.
            for name in ("showrunners", "scripts.production.showrunners"):
                module = sys.modules.get(name)
                if module is not None:
                    for field, value in (
                        ("NOTIFIER_STATE_DIR", self.notifier), ("SESSIONS_DIR", self.sessions),
                        ("SESSIONS", self.session_script),
                    ):
                        _ = scope.enter_context(patch.object(module, field, value))
            return invoke_cli(
                board_json, lambda: board_json.main(list(arguments)), list(arguments),
                self.environment, self.root,
            )

    def generate(self, *arguments: str) -> tuple[dict[str, object], list[dict[str, object]]]:
        result = self.invoke(*arguments)
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
        _, documents = self.generate("--production", fixture.slug)
        self.assertEqual(len(documents), 1)
        units = array_items(documents[0]["units"])
        self.assertEqual(len(units), 1)
        return documents[0], object_fields(units[0])

    def test_cwd_inside_unit_focuses_that_unit_and_only_its_production(self) -> None:
        fixture = self.production()
        other = self.production("other")
        nested = fixture.worktree / "nested"
        nested.mkdir()

        response, documents = self.generate("--cwd", str(nested))

        self.assertEqual(response["files"], [str(self.output / "show.json")])
        self.assertEqual(response["focus"], "alpha")
        self.assertEqual(response["watch"], [str(self.notifier.resolve())])
        self.assertEqual(documents[0]["production"], fixture.slug)
        unit = object_fields(array_items(documents[0]["units"])[0])
        self.assertEqual(unit["key"], "alpha")
        self.assertEqual(unit["plan"], str(fixture.plan))
        self.assertFalse((self.output / f"{other.slug}.json").exists())

    def test_showrunner_checkout_selects_its_production_without_a_unit_focus(self) -> None:
        fixture = self.production()
        _ = self.production("other")

        response, documents = self.generate("--cwd", str(fixture.checkout))

        self.assertIsNone(response["focus"])
        self.assertEqual([doc["production"] for doc in documents], [fixture.slug])

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
        _ = fixture.document.write_text(
            fixture.document.read_text(encoding="utf-8").replace(str(fixture.plan), str(as_built)),
            encoding="utf-8",
        )

        _, unit = self.unit(fixture)

        self.assertEqual(unit["plan"], str(as_built))
        self.assertNotEqual(unit["plan"], str(fixture.plan))

    def test_unrelated_cwd_lists_every_running_production(self) -> None:
        _ = self.production()
        _ = self.production("other")
        _ = self.production("stopped", running=False)

        response, documents = self.generate("--cwd", "/")

        self.assertIsNone(response["focus"])
        self.assertEqual({doc["production"] for doc in documents}, {"show", "other"})
        self.assertFalse((self.output / "stopped.json").exists())

    def test_no_registered_production_resolves_without_output_files(self) -> None:
        response, documents = self.generate("--cwd", "/")
        self.assertEqual(response, {
            "files": [], "focus": None, "watch": [str(self.notifier.resolve())],
        })
        self.assertEqual(documents, [])

    def test_missing_registry_produces_no_discovery_watch_paths(self) -> None:
        self.notifier.rmdir()

        response, documents = self.generate("--cwd", "/")

        self.assertEqual(response, {"files": [], "focus": None, "watch": []})
        self.assertEqual(documents, [])

    def test_output_directory_uses_xdg_then_home_when_no_override_is_set(self) -> None:
        fixture = self.production()
        _ = self.environment.pop("UNIT_BOARD_DIR")
        response, _ = self.generate("--production", fixture.slug)
        self.assertEqual(response["files"], [str(self.root / "state/unit-board/show.json")])
        _ = self.environment.pop("XDG_STATE_HOME")
        response, _ = self.generate("--production", fixture.slug)
        self.assertEqual(response["files"], [str(self.root / ".local/state/unit-board/show.json")])

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
                self.assertEqual(unit["phase"], {"id": "2", "of": 4, "title": "Current delivery"})

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

    def test_activity_stage_and_steps_are_scoped_to_the_open_instance(self) -> None:
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

        _, unit = self.unit(fixture)

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
        self.environment["PLAN_DELEGATE_NOW_EPOCH"] = str(NOW + 60)
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

    def test_gates_and_finish_contract_with_and_without_an_open_phase(self) -> None:
        fixture = self.production()
        for open_phase in (False, True):
            for clears, kind in (("release 2026-10-16 19:00", "timed"),
                                 ("renderer review", "untimed"),
                                 ("cleared 2026-10-16 19:00", "none")):
                with self.subTest(open_phase=open_phase, kind=kind):
                    self.plan(fixture, gate=clears)
                    events = self.delivery_events(open_phase=open_phase)
                    if open_phase:
                        events.append(self.event(
                            "eta_stated", NOW - 600, eta_phase_id="2", eta_at=NOW + 1_800,
                        ))
                    _ = self.write_run(fixture, events)
                    _, unit = self.unit(fixture)
                    rows = [object_fields(row) for row in array_items(unit["phases"])]
                    gate = object_fields(rows[2]["gate"])
                    self.assertEqual(gate["kind"], kind)
                    self.assertEqual(rows[2]["status"], "todo" if kind == "none" else "gated")
                    finish = object_fields(unit["finish"])
                    if kind == "untimed":
                        self.assertEqual(gate, {"kind": "untimed", "gate": "G2", "waits_on": "renderer phase 9"})
                        self.assertEqual(finish, {"kind": "after_gate", "gate": "G2", "waits_on": "renderer phase 9"})
                        for row in rows[2:]:
                            self.assertIsNone(row["start"])
                            self.assertIsNone(row["finish"])
                            self.assertEqual(object_fields(row["gate"])["kind"], "untimed")
                    else:
                        self.assertEqual(finish["kind"], "predicted")
                        self.assertEqual(finish["at"], rows[-1]["finish"])
                        if kind == "timed":
                            self.assertEqual(gate["gate"], "G2")
                            self.assertEqual(gate["waits_on"], "renderer phase 9")
                            self.assertEqual(object_fields(gate["until"])["at"], NOW + 3_600)
                            self.assertEqual(object_fields(rows[2]["start"])["at"], NOW + 3_600)
                        else:
                            self.assertEqual(gate, {"kind": "none"})

    def test_no_run_is_standing_by_with_unknown_finish(self) -> None:
        fixture = self.production()
        _, unit = self.unit(fixture)
        self.assertEqual(unit["state"], "standing-by")
        self.assertEqual(unit["stage"], "Standing by")
        self.assertIsNone(unit["phase"])
        self.assertIsNone(unit["eta"])
        self.assertEqual(unit["finish"], {"kind": "unknown"})

    def test_stage_names_the_gate_when_the_next_unfinished_phase_is_blocked(self) -> None:
        fixture = self.production()
        self.plan(fixture, gate="renderer review")
        text = fixture.plan.read_text(encoding="utf-8").replace("**Blocked by:** G2\n", "")
        _ = fixture.plan.write_text(
            text.replace("### Phase 3", "**Blocked by:** G2\n\n### Phase 3"),
            encoding="utf-8",
        )
        _ = self.write_run(fixture, self.delivery_events(open_phase=False))

        _, unit = self.unit(fixture)

        self.assertIsNone(unit["phase"])
        self.assertEqual(unit["stage"], "Waiting on G2")
        self.assertEqual(object_fields(unit["finish"])["kind"], "after_gate")

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
        self.assertEqual(workers["1"]["model"], "codex/fixture-model")
        doing = str(workers["1"]["doing"])
        self.assertTrue(doing.startswith("building "), doing)
        self.assertLessEqual(len(doing), 80)
        self.assertNotIn("obsolete", doing)
        self.assertEqual(workers["test"]["doing"], "checking the delivery")

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
                         {"id": "8", "of": 3, "title": "Follow-up delivery"})
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
        target = self.output / "show.json"
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
        for clears in ("", "release 2026-10-16 19:00", "renderer review"):
            self.plan(fixture, gate=clears)
            _ = self.write_run(fixture, [*self.delivery_events(), self.event(
                "eta_stated", NOW - 60, eta_phase_id="2", eta_at=NOW + 1_800,
            )])
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


if __name__ == "__main__":
    _ = unittest.main()
