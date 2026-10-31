"""Seed real fixture sources without losing identity or another machine's staff."""
from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock


import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff, staff_sources
from scripts.production.staff_test_support import (
    PROCESS_TIMEOUT, SOURCES_SCRIPT, FixtureMachine, StaffTestCase, production_membership, unit_membership,
)
from scripts.shutdown import pane_record
import herdr
from scripts.production import showrunners
import showrunner_tree
import production_units
from unit_lookup import member_sources


class StaffSourcesTests(StaffTestCase):
    def test_paused_roster_workers_survive_a_director_restart_or_a_missing_messaging_socket(self) -> None:
        _ = self.production("sdlc", "models-unit")
        production_membership("sdlc", "models-unit")
        unit = unit_membership("sdlc", "models-unit")
        _ = self.session_record("session-sdlc", "sdlc")
        registry = self.session_record("session-replacement", "models-unit")
        self.assertEqual(json.loads(registry.read_text())["messagingSocketPath"], "")
        self.pane("w1:models", session_id="session-replacement", environment={"STAFF_ID": unit.id})
        run = self.worker_run("sdlc", "models-unit", "session-former")
        self.write_json(run / "mesh_server.json", {"pid": 0, "codex_home": str(self.root / "codex")})
        roster = self.roster_worker(run, "test", "test", "thread-paused", status="paused")
        history = self.root / "shutdown/account/history/restart.json"
        self.write_json(history, {"session": {
            "session_id": "session-former", "name": "models-unit", "run_dirs": [str(run)],
            "host": {"kind": "unit", "unit": "models-unit", "session_name": "models-unit"},
        }})
        with mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown")}):
            for director in ("session-former", "session-replacement"):
                with self.subTest(director=director):
                    write_text_atomic(run / "director", director + "\n")
                    with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                        _ = self.seed()
                    self.assertEqual(reads.call_count, 1)
                    rows = staff.find(kind="worker", parent=unit.id)
                    self.assertEqual([row.name for row in rows], ["test"])
                    with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                        value = staff_sources.live()
                    self.assertEqual(reads.call_count, 1)
                    members = cast(list[dict[str, object]], value["members"])
                    worker = next(member for member in members if member["id"] == rows[0].id)
                    self.assertEqual(worker["parent"], unit.id)
                    self.assertEqual(worker["identity"], {"codex": "thread-paused"})
                    self.assertEqual(worker["role"], "test")
                    self.assertIn(str(roster), cast(list[str], value["inputs"]))
                    if director == "session-former":
                        self.assertIn(str(history), cast(list[str], value["inputs"]))

    def test_legacy_claude_workers_join_board_slots_by_registered_mesh_name(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("session-sdlc", "sdlc")
        _ = self.session_record("session-models", "models-unit")
        self.pane("w1:models", session_id="session-models", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        for index, (prefix, kind, role) in enumerate((
            ("hana_catalyst", "impl", "test"), ("3D.Project-explicit", "fix", "fix"),
            ("unassigned", "impl", ""),
        )):
            with self.subTest(prefix=prefix, kind=kind, role=role):
                run = self.worker_run("sdlc", f"legacy-{index}", "session-models")
                session_id = f"{index:08x}-worker-session"
                _ = self.session_record(session_id, "Claude worker")
                name = self.legacy_claude_worker(run, session_id, prefix=prefix, kind=kind, role=role)
                self.assertEqual(list(run.glob("impl_worker_*.json")), [])
                _, problems = self.seed()
                row = next((row for row in staff.find(kind="worker") if row.name == name), None)
                with redirect_stderr(io.StringIO()):
                    members = cast(list[dict[str, object]], staff_sources.live()["members"])
                if role:
                    self.assertIsNotNone(row, problems)
                    assert row is not None
                    member = next(member for member in members if member["id"] == row.id)
                    self.assertEqual(member["name"], name)
                    self.assertEqual(member["role"], role)
                    self.assertEqual(member["identity"], {"claude": session_id})
                else:
                    self.assertIsNone(row)
                    self.assertIn(str(run), problems)
                    self.assertFalse(any(member["name"] == name for member in members))

    def test_live_inputs_keep_rejected_production_documents_and_read_them_after_correction(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("session-sdlc", "sdlc")
        rejected = self.production("recovering", "later-unit")
        _ = self.session_record("session-recovering", "recovering")
        _ = self.seed()
        original = rejected.read_bytes()
        _ = rejected.write_bytes(b"\xff invalid production\n")
        with redirect_stderr(io.StringIO()) as problems:
            value = staff_sources.live()
        self.assertIn("production doc could not be read", problems.getvalue())
        self.assertIn(str(rejected), cast(list[str], value["inputs"]))
        self.assertNotIn("recovering", [member["name"] for member in cast(list[dict[str, object]], value["members"])])
        _ = rejected.write_bytes(original)
        with redirect_stderr(io.StringIO()) as problems:
            recovered = staff_sources.live()
        self.assertNotIn(str(rejected), problems.getvalue())
        self.assertIn(str(rejected), cast(list[str], recovered["inputs"]))
        members = cast(list[dict[str, object]], recovered["members"])
        self.assertEqual(next(member["identity"] for member in members if member["name"] == "recovering"),
                         {"claude": "session-recovering"})

    def test_live_inputs_include_present_and_missing_shared_session_configuration(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("session-sdlc", "sdlc")
        _ = self.seed()
        config = Path(os.environ["XDG_CONFIG_HOME"]) / "herdr/shared-session"
        for present in (False, True):
            with self.subTest(present=present):
                if present:
                    config.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(config, "agents\n")
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                    value = staff_sources.live()
                self.assertEqual(reads.call_count, 1)
                inputs = cast(list[str], value["inputs"])
                self.assertIn(str(config), inputs)
                for path in inputs:
                    self.assertFalse(path.startswith(("/proc/", "/dev/", "/usr/share/zoneinfo/")), path)

    def test_pool_lease_distinguishes_unleased_resolved_and_unresolved_units_before_json(self) -> None:
        _ = self.production("sdlc", "models-unit")
        pool = self.pool_reviewer("sdlc", "code1", "thread-pool")
        _ = self.seed()
        states: list[object] = []
        for leased_unit in ("", "models-unit", "missing-unit"):
            fields = cast(dict[str, object], json.loads(pool.read_text()))
            reviewer = cast(dict[str, dict[str, object]], fields["reviewers"])["code1"]
            reviewer["lease"] = {"unit": leased_unit, "ordinal": 1} if leased_unit else None
            self.write_json(pool, fields)
            scan = staff_sources._WorkerSources(staff.snapshot(), member_sources())  # pyright: ignore[reportPrivateUsage]
            with redirect_stderr(io.StringIO()) as problems:
                observations = scan.collect()
            observation = next(item for item in observations if item.name == "code1")
            self.assertIsInstance(observation.pool, staff_sources.ReviewPoolLease)
            assert isinstance(observation.pool, staff_sources.ReviewPoolLease)
            states.append(observation.pool.serves)
            if leased_unit == "missing-unit":
                self.assertIn("missing-unit", problems.getvalue())
        self.assertEqual([type(state).__name__ for state in states],
                         ["PoolUnleased", "PoolServesUnit", "PoolLeaseUnresolved"])
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        self.assertEqual(getattr(states[1], "staff_id"), unit.id)
        self.assertEqual(getattr(states[2], "unit"), "missing-unit")

    def test_finished_and_adopted_rosters_cannot_override_the_live_ledger_worker(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("session-sdlc", "sdlc")
        _ = self.session_record("session-models", "models-unit")
        self.pane("w1:models", session_id="session-models", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        run = self.worker_run("sdlc", "models-unit", "session-models")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "review", "review", "thread-current")
        _ = self.roster_worker(run, "review", "review", "thread-current")
        for number, status in enumerate(("done", "adopted"), start=1):
            old_run = self.worker_run("sdlc", f"history-{status}", "session-models")
            _ = self.roster_worker(old_run, "review", "review", f"thread-{status}", status=status)
            _ = self.roster_worker(old_run, f"fix{number}", "fix", f"thread-only-{status}", status=status)
        gone = self.worker_run("sdlc", "history-gone", "session-ended")
        self.write_json(gone / "progress_history_state.json", {
            "production": "sdlc", "unit": "models-unit",
            "main_agent": {"family": "claude", "session_id": "session-ended"},
        })
        self.write_json(gone / "mesh_server.json", {"pid": 0, "codex_home": str(self.root / "codex")})
        _ = self.roster_worker(gone, "fix3", "fix", "thread-dead-run")
        _, errors = self.seed()
        self.assertNotIn("conflicting worker identities", errors)
        workers = staff.find(kind="worker")
        self.assertEqual([row.name for row in workers], ["review"])
        value = staff_sources.live()
        members = cast(list[dict[str, object]], value["members"])
        reviewer = next(member for member in members if member["id"] == workers[0].id)
        self.assertEqual(reviewer["identity"], {"codex": "thread-current"})
        self.assertIn(str(ledger), cast(list[str], value["inputs"]))

    def test_live_owner_ignores_a_newer_launch_whose_session_has_ended(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.seed()
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        _ = self.session_record("session-sdlc", "sdlc")
        _ = self.session_record("session-running", "models-unit")
        self.pane("w1:running", session_id="session-running", environment={"STAFF_ID": unit.id},
                  written_at="2026-10-30T09:00:00+00:00")
        self.pane("w1:ended", session_id="session-ended", environment={"STAFF_ID": unit.id},
                  written_at="2026-10-31T09:00:00+00:00")
        members = cast(list[dict[str, object]], staff_sources.live()["members"])
        member = next(member for member in members if member["id"] == unit.id)
        self.assertEqual(member["identity"], {"claude": "session-running"})

    def test_live_workers_use_their_producers_without_invented_unit_ledger_entries(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("session-sdlc", "sdlc")
        _ = self.session_record("session-models", "models-unit")
        self.pane("w1:models", session_id="session-models", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        run = self.worker_run("sdlc", "models-unit", "session-models")
        roster = self.roster_worker(run, "impl", "fix", "thread-roster")
        pool = self.pool_reviewer("sdlc", "code1", "thread-pool", unit="models-unit")
        astra = self.astra_worker(run, "sdlc-handler-astra-reviewer", "thread-astra", "session-sdlc")
        design = self.worker_design("sdlc", "sdlc-handler-astra-designer", "thread-design")
        self.assertEqual(list(Path(os.environ["UNIT_WORKERS_STATE"]).rglob("*.json")), [])
        _ = self.seed()
        members = cast(list[dict[str, object]], staff_sources.live()["members"])
        workers = {cast(str, member["name"]): member for member in members if member["kind"] == "worker"}
        self.assertEqual(set(workers), {"impl", "code1", "sdlc-handler-astra-reviewer", "sdlc-handler-astra-designer"})
        self.assertEqual(workers["impl"]["role"], "fix")
        for name, thread in (("impl", "thread-roster"), ("code1", "thread-pool"),
                             ("sdlc-handler-astra-reviewer", "thread-astra"),
                             ("sdlc-handler-astra-designer", "thread-design")):
            self.assertEqual(workers[name]["identity"], {"codex": thread})
        root = next(member for member in members if member["name"] == "sdlc")
        self.assertEqual(workers["code1"]["parent"], root["id"])
        self.assertEqual(workers["sdlc-handler-astra-reviewer"]["parent"], root["id"])
        inputs = cast(list[str], staff_sources.live()["inputs"])
        for path in (roster, pool, astra, design):
            self.assertIn(str(path), inputs)

    def test_unreadable_astra_owner_does_not_seed_a_permanent_unit_owned_duplicate(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("session-sdlc", "sdlc")
        run = self.worker_run("sdlc", "models-unit", "session-models")
        name = "sdlc-handler-astra-reviewer"
        _ = self.ledger_worker(run, "sdlc", "models-unit", name, "review", "thread-astra")
        path = self.astra_worker(run, name, "thread-astra", "session-sdlc")
        original = path.read_text()
        write_text_atomic(path, "{broken")
        _, errors = self.seed()
        self.assertIn(str(path), errors)
        self.assertEqual(staff.find(kind="worker"), [])
        write_text_atomic(path, original)
        _ = self.seed()
        root = staff.by_path("sdlc")
        assert root is not None
        rows = staff.find(kind="worker")
        self.assertEqual([(row.name, row.parent) for row in rows], [(name, root.id)])
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_malformed_pool_reports_its_path_and_preserves_readable_workers(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("session-sdlc", "sdlc")
        _ = self.session_record("session-models", "models-unit")
        self.pane("w1:models", session_id="session-models", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        run = self.worker_run("sdlc", "models-unit", "session-models")
        _ = self.ledger_worker(run, "sdlc", "models-unit", "impl", "impl", "thread-impl")
        pool = self.pool_reviewer("sdlc", "code1", "thread-pool")
        self.write_json(pool, {"generation": 1, "templates": {}, "reviewers": []})
        _, errors = self.seed()
        self.assertIn(str(pool), errors)
        worker = next(row for row in staff.find(kind="worker") if row.name == "impl")
        with redirect_stderr(io.StringIO()) as problems:
            value = staff_sources.live()
        self.assertIn(str(pool), problems.getvalue())
        members = cast(list[dict[str, object]], value["members"])
        self.assertEqual(next(member["identity"] for member in members if member["id"] == worker.id),
                         {"codex": "thread-impl"})

    def test_live_inputs_include_the_remote_owner_record_and_exclude_process_and_system_reads(self) -> None:
        _ = self.tree()
        for machine in ("natedev", "mac"):
            with self.subTest(machine=machine), self.machine(machine):
                if machine == "mac":
                    self.assertNotIn("STAFF_FILE", os.environ)
                _ = self.production("sdlc", "models-unit")
                _ = self.session_record("session-sdlc", "sdlc")
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
                    value = staff_sources.live()
                self.assertEqual(reads.call_count, 1)
                inputs = cast(list[str], value["inputs"])
                self.assertIn(str(self.staff_file), inputs)
                for path in inputs:
                    self.assertFalse(path.startswith(("/proc/", "/dev/", "/usr/share/zoneinfo/")), path)
        self.assertEqual(len(self.ssh_commands()), 1)

    def seed(self, *, dry_run: bool = False) -> tuple[str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            staff_sources.seed(dry_run=dry_run)
        return output.getvalue(), errors.getvalue()

    def test_worker_seed_imports_ledger_and_roster_once_and_keeps_replacement_identity(self) -> None:
        _ = self.production("sdlc", "models-unit")
        run = self.worker_run("sdlc", "models-unit", "session-models")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "impl", "impl", "thread-first")
        _ = self.ledger_worker(run, "sdlc", "models-unit", "test", "test", "session-test", family="claude")
        _ = self.ledger_worker(run, "sdlc", "models-unit", "fix3", "fix", "thread-ended", retired=True)
        roster_path = run / "mesh_roster.json"
        roster = cast(dict[str, object], json.loads(roster_path.read_text()))
        roster["review2"] = {"role": "review", "status": "paused", "thread_id": "thread-roster"}
        self.write_json(roster_path, roster)
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads, \
                mock.patch.object(staff, "apply_seed", wraps=staff.apply_seed) as writes:
            _ = self.seed()
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(writes.call_count, 1)
        unit = staff.by_path("sdlc/models")
        assert unit is not None
        workers = {row.name: row for row in staff.find(kind="worker", parent=unit.id)}
        self.assertEqual(set(workers), {"impl", "test", "review2"})
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)
        document = cast(dict[str, object], json.loads(ledger.read_text()))
        entries = cast(dict[str, dict[str, object]], document["workers"])
        backend = cast(dict[str, object], entries["impl"]["backend"])
        backend["thread_id"] = "thread-replacement"
        self.write_json(ledger, document)
        _ = self.seed()
        self.assertEqual(staff.by_id(workers["impl"].id), workers["impl"])
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_worker_seed_names_each_unreadable_source_instead_of_silently_skipping_it(self) -> None:
        _ = self.production("sdlc", "models-unit")
        run = self.worker_run("sdlc", "models-unit", "session-models")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "impl", "impl", "thread-impl")
        pool = self.pool_reviewer("sdlc", "code1", "thread-pool", unit="models-unit")
        astra = self.astra_worker(run, "sdlc-handler-astra-reviewer", "thread-astra", "session-sdlc")
        _ = self.seed()
        for path in (ledger, run / "mesh_roster.json", pool, astra):
            original = path.read_text()
            with self.subTest(path=path):
                write_text_atomic(path, "{broken")
                with redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                    try:
                        staff_sources.seed()
                    except staff.StaffUnreadable as error:
                        print(error, file=sys.stderr)
                self.assertIn(str(path), errors.getvalue())
                self.assertTrue(errors.getvalue().strip())
                write_text_atomic(path, original)

    def test_live_staff_command_keeps_pool_ownership_identity_and_all_input_paths(self) -> None:
        doc = self.production("sdlc", "models-unit")
        self.pane("w1:runner", session_id="session-sdlc", environment={})
        self.pane("w1:models", session_id="session-models", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        registry = [self.session_record("session-sdlc", "sdlc"),
                    self.session_record("session-models", "models-unit"),
                    self.session_record("session-test", "test")]
        run = self.worker_run("sdlc", "models-unit", "session-models")
        ledger = self.ledger_worker(run, "sdlc", "models-unit", "impl", "impl", "thread-impl")
        _ = self.ledger_worker(run, "sdlc", "models-unit", "test", "test", "session-test", family="claude")
        pool = self.pool_reviewer("sdlc", "code1", "thread-pool", unit="models-unit", busy_pid=os.getpid())
        astra = self.astra_worker(run, "sdlc-handler-astra-reviewer", "thread-astra", "session-sdlc")
        _ = self.seed()
        before = self.staff_file.read_bytes()
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as reads, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(staff.main(["show", "--json", "--live"]), 0)
        self.assertEqual(reads.call_count, 1)
        document = cast(dict[str, object], json.loads(output.getvalue()))
        self.assertEqual(set(document), {"schema", "inputs", "members"})
        self.assertEqual(document["schema"], 2)
        members = cast(list[dict[str, object]], document["members"])
        by_name = {cast(str, member["name"]): member for member in members}
        by_identity = {tuple(cast(dict[str, str], member["identity"]).items()): member for member in members}
        root = by_name["sdlc"]
        unit = by_name["models"]
        self.assertEqual(root["identity"], {"claude": "session-sdlc"})
        self.assertEqual(unit["parent"], root["id"])
        self.assertEqual(unit["identity"], {"claude": "session-models"})
        for name, identity in (("impl", {"codex": "thread-impl"}), ("test", {"claude": "session-test"})):
            self.assertEqual(by_name[name]["parent"], unit["id"])
            self.assertEqual(by_name[name]["identity"], identity)
            self.assertNotIn("pool", by_name[name])
        reviewer = by_identity[(("codex", "thread-pool"),)]
        self.assertEqual(reviewer["parent"], root["id"])
        self.assertEqual(reviewer["pool"], {"state": "busy", "serves": unit["id"]})
        designer = by_identity[(("codex", "thread-astra"),)]
        self.assertEqual(designer["parent"], root["id"])
        self.assertEqual(designer["role"], "astra-reviewer")
        ids = {member["id"] for member in members}
        self.assertEqual(len(ids), len(members))
        for member in members:
            self.assertEqual(set(member), {"id", "kind", "name", "parent", "role", "identity"}
                             | ({"pool"} if member is reviewer else set()))
            self.assertIn(member["role"], {"showrunner", "unit", "impl", "test", "review", "fix", "astra-designer", "astra-reviewer"})
            self.assertTrue(member["parent"] in ids or member is root)
            self.assertEqual(member["parent"] is None, member is root)
            self.assertEqual(len(cast(dict[str, str], member["identity"])), 1)
        inputs = cast(list[str], document["inputs"])
        self.assertTrue(all(Path(path).is_absolute() for path in inputs))
        self.assertEqual(len(inputs), len(set(inputs)))
        for path in (self.staff_file, doc, ledger, pool, astra, run / "mesh_roster.json", *registry,
                     Path(os.environ["NOTIFIER_STATE_DIR"]) / "showrunner-sdlc" / "conf",
                     pane_record.record_path(herdr.shared_session())):
            self.assertIn(str(path), inputs)
        self.assertEqual(self.staff_file.read_bytes(), before)
        ledger_value = cast(dict[str, object], json.loads(ledger.read_text()))
        workers = cast(dict[str, dict[str, object]], ledger_value["workers"])
        cast(dict[str, object], workers["impl"]["backend"])["thread_id"] = "thread-replacement"
        self.write_json(ledger, ledger_value)
        replacement = cast(dict[str, object], json.loads(self.cli("show", "--json", "--live").stdout))
        replacement_members = cast(list[dict[str, object]], replacement["members"])
        replaced = next(member for member in replacement_members if member["id"] == by_name["impl"]["id"])
        self.assertEqual(replaced["identity"], {"codex": "thread-replacement"})
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_live_pool_busy_state_follows_the_pid_without_changing_staff(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _ = self.session_record("session-sdlc", "sdlc")
        pool = self.pool_reviewer("sdlc", "code1", "thread-pool", unit="models-unit", busy_pid=os.getpid())
        _ = self.seed()
        before = self.staff_file.read_bytes()
        states: list[str] = []
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as busy:
            value = cast(dict[str, object], json.loads(pool.read_text()))
            reviewers = cast(dict[str, dict[str, object]], value["reviewers"])
            reviewers["code1"]["busy_pid"] = busy.pid
            self.write_json(pool, value)
            pool_before = pool.read_bytes()
            for finished in (False, True):
                if finished:
                    _ = busy.communicate(timeout=PROCESS_TIMEOUT)
                document = cast(dict[str, object], json.loads(self.cli("show", "--json", "--live").stdout))
                members = cast(list[dict[str, object]], document["members"])
                member = next(member for member in members if member.get("identity") == {"codex": "thread-pool"})
                states.append(cast(str, cast(dict[str, object], member["pool"])["state"]))
                self.assertEqual(pool.read_bytes(), pool_before)
        self.assertEqual(states, ["busy", "idle"])
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_preserves_recorded_moves_despite_stale_sources(self) -> None:
        old = staff.ensure("showrunner", name="old")
        new = staff.ensure("showrunner", name="new")
        child = staff.ensure("showrunner", name="tools", parent=old.id)
        unit = staff.ensure("unit", name="models", parent=old.id)
        worker = staff.ensure("worker", name="impl1", parent=unit.id)
        _ = self.production("old", "models-unit")
        _ = self.production("new")
        _ = self.production("child", workspace="tools", parent="old")
        moved_unit = staff.move(unit.id, new.id)
        moved_child = staff.move(child.id, new.id)
        before = self.staff_file.read_bytes()
        output, errors = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIn("unchanged", errors)
        self.assertNotIn("add ", output)
        self.assertEqual(staff.by_id(unit.id), moved_unit)
        self.assertEqual(staff.by_id(child.id), moved_child)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(showrunner_tree.parent_of("child"), "new")
        self.assertEqual(showrunner_tree.children_of("new"), ["child"])
        self.assertEqual(showrunner_tree.children_of("old"), [])

    def test_reading_a_snapshot_never_collects_seed_sources(self) -> None:
        expected = self.tree()
        with mock.patch.object(staff_sources, "_collect", side_effect=AssertionError("read ran seed")):
            record = staff.snapshot()
            self.assertCountEqual(record.rows, expected)

    def test_seed_rerun_uses_timers_parent_files_docs_and_pane_records(self) -> None:
        _ = self.production("parent", workspace="umbrella")
        _ = self.production("sdlc", "models-unit", workspace="tools", parent="parent")
        self.pane("w1:p1", session_id="session-sdlc", environment={})
        self.pane("w1:p2", session_id="unit-session", environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        first = self.script(SOURCES_SCRIPT, "seed")
        before = self.staff_file.read_bytes()
        second = self.script(SOURCES_SCRIPT, "seed")
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIn("unchanged", second.stderr)
        self.assertIn("tools", first.stdout)
        self.assertIn("models", first.stdout)
        root = staff.by_path("umbrella")
        child = staff.by_path("tools")
        unit = staff.by_path("tools/models")
        assert root is not None and child is not None and unit is not None
        self.assertIsNone(child.parent)
        self.assertEqual(unit.parent, child.id)
        self.assertEqual(len(staff.find()), 3)
        self.assertEqual(staff.find(kind="worker"), [])

    def test_seed_keeps_renamed_unit_and_showrunner_by_pane_staff_ids(self) -> None:
        showrunner, unit, worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        renamed_unit = staff.rename(unit.id, "panes")
        renamed_showrunner = staff.rename(showrunner.id, "control")
        self.pane("w1:p1", session_id="session-sdlc", environment={"STAFF_ID": showrunner.id})
        self.pane("w1:p2", session_id="models-session", environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        _ = self.seed()
        before = self.staff_file.read_bytes()
        _output, errors = self.seed()
        self.assertIn("unchanged", errors)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.find(kind="showrunner"), [renamed_showrunner])
        self.assertEqual(staff.find(kind="unit"), [renamed_unit])
        self.assertEqual(staff.by_path("control/panes/impl1"), worker)
        self.assertEqual(len(staff.find()), 3)

    def test_member_result_is_explicit_and_uses_only_the_supplied_snapshot(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        record = staff.snapshot()
        missing = staff_sources.UnitMember("sdlc", "missing")
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("member reread the staff file")):
            self.assertEqual(staff_sources.member(missing, record), staff_sources.NoSuchMember(missing))
            self.assertEqual(staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), record), staff_sources.MemberFound(showrunner))
            self.assertEqual(staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), record), staff_sources.MemberFound(unit))

    def test_member_uses_newest_matching_launch_and_id_before_name(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        renamed = staff.rename(unit.id, "panes")
        fallback = staff.ensure("unit", name="models", parent=showrunner.id)
        self.pane("w1:old", session_id="old-unit", written_at="2026-10-29T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.pane("w1:new", session_id="new-unit", environment={
            "STAFF_ID": renamed.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        self.pane("w1:other", session_id="other-unit", written_at="2026-10-31T09:00:00+00:00", environment={
            "STAFF_ID": fallback.id, "SHOWRUNNER_UNIT": "other", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        result = staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), staff.snapshot())
        self.assertEqual(result, staff_sources.MemberFound(renamed))

    def test_member_with_no_staff_id_matches_unit_by_short_name(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = self.production("sdlc", "models-unit")
        self.pane("w1:p2", session_id="unit-session", environment={"SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit"})
        record = staff.snapshot()
        self.assertEqual(staff_sources.member(staff_sources.UnitMember("sdlc", "models-unit"), record), staff_sources.MemberFound(unit))
        self.assertEqual(staff_sources.member(staff_sources.ShowrunnerMember("sdlc"), record), staff_sources.MemberFound(showrunner))

    def test_legacy_member_follows_a_transfer_without_using_the_production_mark(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        _ = self.production("receiving")
        receiving = staff.ensure("showrunner", name="receiving")
        moved = staff.move(unit.id, receiving.id)
        self.pane("w1:unit", session_id="unit-session", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        record = staff.snapshot()
        for slug in ("sdlc", "missing-source"):
            with self.subTest(slug=slug):
                self.assertEqual(staff_sources.member(staff_sources.UnitMember(slug, "models-unit"), record),
                                 staff_sources.NoSuchMember(staff_sources.UnitMember(slug, "models-unit")))
        self.assertEqual(staff_sources.member(staff_sources.UnitMember("receiving", "models-unit"), record),
                         staff_sources.MemberFound(moved))
        self.assertEqual(staff_sources.unit_parent("models-unit", "unit-session"),
                         staff_sources.UnitProduction("receiving", str(self.source_root / "receiving-production.md")))

    def test_production_member_rejects_a_moved_row_despite_its_old_id_bearing_launch(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        _ = self.production("receiving", workspace="control")
        receiving = staff.ensure("showrunner", name="control")
        moved = staff.move(unit.id, receiving.id)
        self.pane("w1:unit", session_id="unit-session", environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.NoSuchMember(selector))
        self.assertEqual(staff_sources.unit_parent("models-unit", "unit-session"),
                         staff_sources.UnitProduction("receiving", str(self.source_root / "receiving-production.md")))
        self.assertEqual(staff.by_id(unit.id), moved)

    def test_observed_id_bearing_pane_follows_its_current_parent(self) -> None:
        _original, unit, _worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        new_doc = self.production("receiving")
        receiving = staff.ensure("showrunner", name="receiving")
        moved = staff.move(unit.id, receiving.id)
        self.pane("w1:unit", session_id="unit-session", environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        other = staff.ensure("showrunner", name="other")
        _ = staff.ensure("unit", name="models", parent=other.id)
        self.assertEqual(staff_sources.member(staff_sources.ObservedUnit("models-unit", unit.id), staff.snapshot()),
                         staff_sources.MemberFound(moved))
        self.assertEqual(staff_sources.unit_parent("models-unit", "unit-session"),
                         staff_sources.UnitProduction("receiving", str(new_doc)))

    def test_metadata_from_the_current_parent_wins_over_an_old_same_name_row(self) -> None:
        _original, unit, _worker = self.tree()
        old_doc = self.production("sdlc", "models-unit")
        new_doc = self.production("receiving", "models-unit")
        receiving = staff.ensure("showrunner", name="receiving")
        _ = staff.move(unit.id, receiving.id)
        new_text = new_doc.read_text().replace(str(self.source_root / "worktrees" / "models-unit"),
                                              str(self.source_root / "current-worktree")).replace("active", "current-plan.md")
        write_text_atomic(new_doc, new_text)
        expected = production_units.unit_table(new_text.splitlines())[0]
        actual = production_units.live_unit_table(new_text.splitlines(), "receiving")
        self.assertEqual([(row["Worktree"], row["Plan"]) for row in actual],
                         [(expected["Worktree"], expected["Plan"])])
        self.assertNotEqual(expected["Worktree"], production_units.unit_table(old_doc.read_text().splitlines())[0]["Worktree"])

    def test_competing_orphan_metadata_is_unavailable_instead_of_picking_a_document(self) -> None:
        _original, unit, _worker = self.tree()
        for slug in ("sdlc", "former"):
            document = self.production(slug, "models-unit")
            write_text_atomic(document, document.read_text().replace("active", f"{slug}-plan.md"))
        new_doc = self.production("receiving")
        receiving = staff.ensure("showrunner", name="receiving")
        _ = staff.move(unit.id, receiving.id)
        with redirect_stderr(io.StringIO()) as errors:
            rows = production_units.live_unit_table(new_doc.read_text().splitlines(), "receiving")
        self.assertEqual(len(rows), 1)
        self.assertEqual(production_units.cell_value(rows[0]["Worktree"]), "—")
        self.assertEqual(production_units.cell_value(rows[0]["Plan"]), "—")
        self.assertRegex(errors.getvalue(), "(?i)models.*(ambiguous|unavailable)|(ambiguous|unavailable).*models")

    def test_ambiguous_legacy_member_never_uses_the_mark_to_choose_a_row(self) -> None:
        _ = self.production("sdlc", "models-unit")
        _original, _unit, _worker = self.tree()
        receiving = staff.ensure("showrunner", name="receiving")
        _ = staff.ensure("unit", name="models", parent=receiving.id)
        self.pane("w1:unit", session_id="unit-session", environment={
            "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        selector = staff_sources.ObservedUnit("models-unit")
        with self.assertRaisesRegex(staff.StaffUnreadable, "models.*no unique staff entry"):
            _ = staff_sources.member(selector, staff.snapshot())

    def test_mac_unit_parent_uses_one_owner_read_for_the_whole_resolution(self) -> None:
        _original, unit, _worker = self.tree()
        with self.machine("mac"):
            _ = self.production("sdlc", "models-unit")
            _ = self.production("receiving")
            receiving = staff.ensure("showrunner", name="receiving")
            _ = staff.move(unit.id, receiving.id)
            self.pane("w1:unit", session_id="unit-session", environment={
                "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
            })
            before = len(self.ssh_commands())
            parent = staff_sources.unit_parent("models-unit", "unit-session")
            self.assertEqual(parent.slug, "receiving")
            self.assertEqual(len(self.ssh_commands()) - before, 1)

    def test_transferred_membership_keeps_worktree_and_plan_metadata_from_the_old_doc(self) -> None:
        original, unit, _worker = self.tree()
        old_doc = self.production("sdlc", "models-unit")
        new_doc = self.production("receiving")
        receiving = staff.ensure("showrunner", name="receiving")
        _ = staff.move(unit.id, receiving.id)
        record = staff.snapshot()
        tables = production_units
        old_lines, new_lines = old_doc.read_text().splitlines(), new_doc.read_text().splitlines()
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("second owner read")):
            old_rows = tables.live_unit_table(old_lines, "sdlc", record=record)
            new_rows = tables.live_unit_table(new_lines, "receiving", record=record)
            retired = tables.retired_units(old_lines, "sdlc", record=record)
            old_marks = tables.ProductionMembers("sdlc", record=record)
            new_marks = tables.ProductionMembers("receiving", record=record)
            cells = tables.unit_table(old_lines)[0]
            self.assertTrue(tables.unit_is_retired(cells, old_marks))
            self.assertFalse(tables.unit_is_retired(cells, new_marks))
        self.assertEqual(old_rows, [])
        self.assertEqual(len(new_rows), 1)
        self.assertEqual(tables.cell_value(new_rows[0]["Unit"]), "models-unit")
        self.assertEqual(new_rows[0]["Worktree"], cells["Worktree"])
        self.assertEqual(new_rows[0]["Plan"], cells["Plan"])
        self.assertEqual(retired, {"models-unit"})
        self.assertEqual(record.find(kind="unit", parent=original.id), [])

    def test_unit_parent_agrees_with_showrunner_members_after_transfer(self) -> None:
        original, unit, _worker = self.tree()
        old_doc = self.production("sdlc", "models-unit", workspace="sdlc")
        new_doc = self.production("receiving", workspace="new-tools")
        destination = staff.ensure("showrunner", name="new-tools")
        destination = staff.rename(destination.id, "control")
        self.pane("w1:showrunner", session_id="session-receiving", environment={"STAFF_ID": destination.id})
        self.pane("w1:unit", session_id="unit-session", environment={
            "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
        })
        for slug, expected_parent, doc in (("sdlc", original, old_doc), ("receiving", destination, new_doc)):
            with self.subTest(slug=slug):
                _ = staff.move(unit.id, expected_parent.id)
                record = staff.snapshot()
                members = staff_sources.showrunner_members(record)
                with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as snapshot, \
                        mock.patch.object(pane_record, "read_strict", wraps=pane_record.read_strict) as launches, \
                        mock.patch.object(showrunners, "read_sources", wraps=showrunners.read_sources) as productions:
                    parent = staff_sources.unit_parent("models-unit", "unit-session")
                self.assertEqual(parent, staff_sources.UnitProduction(slug, str(doc)))
                self.assertEqual(members[parent.slug], expected_parent)
                recorded_unit = record.by_id(unit.id)
                assert recorded_unit is not None
                self.assertEqual(members[parent.slug].id, recorded_unit.parent)
                self.assertEqual(snapshot.call_count, 1)
                self.assertEqual(launches.call_count, 1)
                self.assertEqual(productions.call_count, 1)

    def test_member_with_unresolved_or_wrong_kind_id_does_not_take_an_unrelated_name(self) -> None:
        showrunner, _unit, worker = self.tree()
        _ = self.production("sdlc", "models-unit")
        selector = staff_sources.UnitMember("sdlc", "models-unit")
        for staff_id in ("s00000000000", showrunner.id, worker.id):
            with self.subTest(staff_id=staff_id):
                self.pane("w1:p2", session_id="unit-session", environment={
                    "STAFF_ID": staff_id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "models-unit",
                })
                self.assertEqual(staff_sources.member(selector, staff.snapshot()), staff_sources.NoSuchMember(selector))

    def test_mac_seed_reads_local_sources_and_sends_one_input_to_owner(self) -> None:
        _ = self.production("owner-only", "owner-unit")
        with self.machine("mac"):
            _ = self.production("mac-only", "laptop-unit", workspace="laptop")
            self.pane("w1:p1", session_id="session-mac-only", environment={})
            _ = self.seed()
            self.assertFalse(self.mac_file.exists())
            self.assertTrue((self.mac_file.parent / "operation.lock").exists())
        self.assertIsNotNone(staff.by_path("laptop/laptop"))
        self.assertIsNone(staff.by_path("owner-only"))
        self.assertEqual({row.name for row in staff.find()}, {"laptop"})
        self.assertEqual(self.ssh_commands(), [["show", "--snapshot", "--json"], ["apply-seed", "--json"]])
        self.assertTrue((self.staff_file.parent / "operation.lock").exists())
        self.assertNotEqual((self.staff_file.parent / "operation.lock").stat().st_ino, (self.mac_file.parent / "operation.lock").stat().st_ino)

    def test_each_seed_order_preserves_other_machine_units_and_workers(self) -> None:
        for first in ("natedev", "mac"):
            with self.subTest(first=first):
                showrunner = staff.ensure("showrunner", name="sdlc")
                alpha = staff.ensure("unit", name="alpha", parent=showrunner.id)
                beta = staff.ensure("unit", name="beta", parent=showrunner.id)
                alpha_worker = staff.ensure("worker", name="impl1", parent=alpha.id)
                beta_worker = staff.ensure("worker", name="impl1", parent=beta.id)
                layouts: tuple[tuple[FixtureMachine, str], ...] = (("natedev", "alpha-unit"), ("mac", "beta-unit"))
                for machine, local_unit in layouts:
                    with self.machine(machine):
                        doc = self.production("sdlc")
                        self.units_doc(doc, ("alpha-unit", "beta-unit"), workspace="sdlc")
                        _ = self.worktree(local_unit)
                order: tuple[FixtureMachine, FixtureMachine] = ("natedev", "mac") if first == "natedev" else ("mac", "natedev")
                for machine in order:
                    with self.machine(machine):
                        _ = self.seed()
                    self.assertCountEqual(staff.find(), [showrunner, alpha, beta, alpha_worker, beta_worker])

    def test_retirement_requires_positive_evidence_and_keeps_workers(self) -> None:
        showrunner, unit, worker = self.tree()
        empty = staff.ensure("unit", name="empty", parent=showrunner.id)
        omitted = staff.ensure("unit", name="omitted", parent=showrunner.id)
        remote = staff.ensure("unit", name="remote", parent=showrunner.id)
        unregistered = staff.ensure("showrunner", name="other-machine")
        doc = self.production("sdlc", "models-unit", "empty-unit")
        self.units_doc(doc, ("models-unit", "empty-unit", "remote-unit"), workspace="sdlc", retired=("models-unit", "empty-unit"))
        before = self.staff_file.read_bytes()
        _output, errors = self.seed()
        self.assertEqual(staff.by_id(empty.id), empty)
        self.assertEqual(staff.by_id(omitted.id), omitted)
        self.assertEqual(staff.by_id(remote.id), remote)
        self.assertEqual(staff.by_id(unit.id), unit)
        self.assertEqual(staff.by_id(worker.id), worker)
        self.assertEqual(staff.by_id(unregistered.id), unregistered)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(errors, "unchanged\n")

    def test_unreadable_doc_keeps_its_rows_and_reports_one_source_line(self) -> None:
        _ = self.tree()
        doc = self.production("sdlc", "models-unit")
        before = self.staff_file.read_bytes()
        doc.unlink()
        doc.mkdir()
        _output, errors = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)
        lines = [line for line in errors.splitlines() if "sdlc" in line]
        self.assertEqual(len(lines), 1, errors)

    def test_dry_run_describes_changes_without_applying_them(self) -> None:
        _ = self.tree()
        _ = self.production("sdlc", "models-unit", "new-unit")
        before = self.staff_file.read_bytes()
        with mock.patch.object(staff, "apply_seed", wraps=staff.apply_seed) as apply:
            output, _errors = self.seed(dry_run=True)
        apply.assert_not_called()
        self.assertIn("new", output)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIsNone(staff.by_path("sdlc/new"))

    def test_seed_uses_one_snapshot_and_applies_all_sources_in_one_write(self) -> None:
        _ = self.write_document({"schema": 1, "rows": []})
        _ = self.production("one", "alpha-unit")
        _ = self.production("two", "beta-unit")
        with mock.patch.object(staff, "snapshot", wraps=staff.snapshot) as read, \
                mock.patch.object(staff, "apply_seed", wraps=staff.apply_seed) as apply:
            _ = self.seed()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(apply.call_count, 1)
        self.assertIsNotNone(staff.by_path("one/alpha"))
        self.assertIsNotNone(staff.by_path("two/beta"))

    def test_seed_cli_reports_an_unreadable_record_without_replacing_it(self) -> None:
        before = self.write_document({"schema": 2, "rows": []})
        _ = self.production("sdlc", "models-unit")
        failed = self.script(SOURCES_SCRIPT, "seed", code=2)
        self.assertTrue(failed.stderr.strip())
        self.assertNotIn("Traceback", failed.stderr)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_waits_for_lifecycle_operation_then_reads_updated_docs(self) -> None:
        _ = self.write_document({"schema": 1, "rows": []})
        old_doc = self.production("old", "models-unit")
        new_doc = self.production("new")
        _ = self.seed()
        unit = staff.by_path("old/models")
        destination = staff.by_path("new")
        assert unit is not None and destination is not None
        lock = self.staff_file.parent / "operation.lock"
        with self.lock_notice(lock) as listener:
            with staff.operation():
                moved = staff.move(unit.id, destination.id)
                pending = self.start_script(SOURCES_SCRIPT, "seed")
                self.assert_operation_blocks(pending, listener)
                self.units_doc(old_doc, (), workspace="old")
                self.units_doc(new_doc, ("models-unit",), workspace="new")
            _output, errors = pending.communicate(timeout=PROCESS_TIMEOUT)
        self.assertEqual(pending.returncode, 0, errors)
        self.assertEqual(staff.by_id(unit.id), moved)
        self.assertEqual(staff.by_path("new/models"), moved)
        self.assertIsNone(staff.by_path("old/models"))

    def test_apply_seed_keeps_current_names_and_refuses_an_invalid_transaction_atomically(self) -> None:
        first = staff.ensure("showrunner", name="first")
        second = staff.ensure("showrunner", name="second")
        seed = staff.SeedInput((
            staff.SeedMember("first", "showrunner", "first", first.id, staff.RootSeedParent()),
            staff.SeedMember("second", "showrunner", "second", second.id, staff.SeedParent("first")),
        ))
        renamed = staff.rename(second.id, "renamed")
        record = staff.apply_seed(seed)
        self.assertEqual(record.by_id(second.id), replace(renamed, parent=first.id))
        invalid = staff.SeedInput((
            staff.SeedMember("first", "showrunner", "first", first.id, staff.SeedParent("second")),
            staff.SeedMember("second", "showrunner", "second", second.id, staff.SeedParent("first")),
        ))
        before = self.staff_file.read_bytes()
        with self.assertRaises(staff.StaffRefused):
            _ = staff.apply_seed(invalid)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_keeps_a_renamed_unit_whose_identity_is_only_on_the_other_machine(self) -> None:
        showrunner = staff.ensure("showrunner", name="sdlc")
        unit = staff.ensure("unit", name="beta", parent=showrunner.id)
        renamed = staff.rename(unit.id, "panes")
        doc = self.production("sdlc")
        self.units_doc(doc, ("beta-unit",), workspace="sdlc")
        with self.machine("mac"):
            _ = self.production("sdlc", "beta-unit")
            self.pane("w1:beta", session_id="beta-session", environment={
                "STAFF_ID": unit.id, "SHOWRUNNER_UNIT": "sdlc", "SHOWRUNNER_UNIT_ID": "beta-unit",
            })
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(staff.find(kind="unit"), [renamed])
        self.assertEqual(self.staff_file.read_bytes(), before)
        with self.machine("mac"):
            _ = self.seed()
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertEqual(staff.by_id(unit.id), renamed)

    def test_bad_pane_evidence_prevents_every_seed_change_and_names_the_file(self) -> None:
        showrunner, unit, _ = self.tree()
        _ = staff.rename(unit.id, "panes")
        _ = self.production("parent")
        _ = self.production("sdlc", "models-unit", "new-unit", parent="parent")
        session = herdr.shared_session()
        path = pane_record.record_path(session)
        for content in ("{broken", "[]", None):
            with self.subTest(content=content):
                if content is None:
                    path.unlink()
                    path.mkdir()
                else:
                    write_text_atomic(path, content)
                before = self.staff_file.read_bytes()
                failed = self.script(SOURCES_SCRIPT, "seed", code=2)
                self.assertEqual(self.staff_file.read_bytes(), before)
                self.assertEqual(staff.by_id(showrunner.id), showrunner)
                self.assertEqual(failed.stdout, "")
                self.assertEqual(len(failed.stderr.splitlines()), 1, failed.stderr)
                self.assertTrue(failed.stderr.startswith(f"StaffUnreadable: {path}:"), failed.stderr)
                self.assertEqual(pane_record.read(session), {})
                if content is None:
                    path.rmdir()

    def test_malformed_pane_entries_refuse_seed_but_keep_legacy_read_results(self) -> None:
        showrunner = staff.ensure("showrunner", name="sdlc")
        _ = staff.ensure("unit", name="old", parent=showrunner.id)
        _ = self.production("sdlc", "new-unit")
        session = herdr.shared_session()
        path = pane_record.record_path(session)
        before = self.staff_file.read_bytes()
        valid: dict[str, object] = {
            "session_id": "session-sdlc", "environment": {}, "written_at": "2026-10-30T09:00:00+00:00",
        }
        malformed: tuple[object, ...] = (
            None, {}, {**valid, "environment": None}, {**valid, "environment": {"STAFF_ID": 7}},
            {**valid, "session_id": 7}, {**valid, "written_at": None},
        )
        for entry in malformed:
            with self.subTest(entry=entry):
                document = {"w1:p1": entry}
                self.write_json(path, document)
                self.assertEqual(pane_record.read(session), document)
                with self.assertRaises(ValueError) as raised:
                    _ = pane_record.read_strict(session)
                self.assertIn(str(path), str(raised.exception))
                self.assertIn("w1:p1", str(raised.exception))
                failed = self.script(SOURCES_SCRIPT, "seed", code=2)
                self.assertEqual(self.staff_file.read_bytes(), before)
                self.assertEqual(failed.stdout, "")
                self.assertEqual(len(failed.stderr.splitlines()), 1, failed.stderr)
                self.assertTrue(failed.stderr.startswith(f"StaffUnreadable: {path}:"), failed.stderr)
                self.assertIn("w1:p1", failed.stderr)
                self.assertEqual(pane_record.read(session), document)

    def test_seed_keeps_a_parent_whose_timer_is_on_the_other_machine(self) -> None:
        parent = staff.ensure("showrunner", name="remote")
        child = staff.ensure("showrunner", name="local", parent=parent.id)
        _ = self.production("local", parent="remote")
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_seed_roots_a_showrunner_only_when_parent_evidence_says_root(self) -> None:
        parent = staff.ensure("showrunner", name="parent")
        child = staff.ensure("showrunner", name="child", parent=parent.id)
        _ = self.production("parent", parent="child")
        _ = self.production("child", parent="parent")
        before = self.staff_file.read_bytes()
        _ = self.seed()
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(staff.by_id(parent.id), parent)
        self.assertEqual(self.staff_file.read_bytes(), before)
        _ = staff.move(child.id, parent.id)
        parent_file = showrunners.SHOWRUNNER_STATE_DIR / "child" / showrunners.PARENT_FILE
        parent_file.unlink()
        _ = self.seed()
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_unreadable_parent_evidence_leaves_its_production_unchanged(self) -> None:
        showrunner = self.tree()[0]
        _ = self.production("sdlc", "models-unit", "new-unit")
        path = showrunners.SHOWRUNNER_STATE_DIR / "sdlc" / showrunners.PARENT_FILE
        path.mkdir(parents=True)
        self.assertEqual(showrunner_tree.parent_of("sdlc"), "")
        self.assertEqual(showrunner_tree.parent_of("sdlc"), "")
        _output, errors = self.seed()
        self.assertIsNotNone(staff.by_path("sdlc/new"))
        self.assertEqual(staff.by_id(showrunner.id), showrunner)
        self.assertEqual(errors, "")

    def test_seed_reads_each_source_once_and_reports_a_bad_doc_once(self) -> None:
        _ = self.write_document({"schema": 1, "rows": []})
        docs = [self.production("one", "alpha-unit", "beta-unit"), self.production("two", "gamma-unit")]
        bad = self.production("bad")
        bad.unlink()
        bad.mkdir()
        self.pane("w1:one", session_id="session-one", environment={})
        paths = [*docs, bad, pane_record.record_path(herdr.shared_session())]
        for slug in ("one", "two", "bad"):
            paths.append(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}" / "conf")
        parent_paths = [showrunners.SHOWRUNNER_STATE_DIR / slug / showrunners.PARENT_FILE
                        for slug in ("one", "two")]
        reads: Counter[Path] = Counter()
        read_text = Path.read_text

        def counted(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            reads[path] += 1
            return read_text(path, encoding=encoding, errors=errors)

        with mock.patch.object(Path, "read_text", new=counted):
            _output, errors = self.seed()
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(reads[path], 1)
        for path in parent_paths:
            with self.subTest(path=path):
                self.assertEqual(reads[path], 0)
        self.assertEqual(len([line for line in errors.splitlines() if str(bad) in line]), 1, errors)
        for path in ("one/alpha", "one/beta", "two/gamma"):
            self.assertIsNotNone(staff.by_path(path))

    def test_an_unreadable_timer_does_not_hide_other_productions(self) -> None:
        old = staff.ensure("showrunner", name="bad")
        old_unit = staff.ensure("unit", name="kept", parent=old.id)
        _ = self.production("bad")
        _ = self.production("good", "models-unit")
        conf = showrunners.NOTIFIER_STATE_DIR / "showrunner-bad" / "conf"
        conf.unlink()
        conf.mkdir()
        _output, errors = self.seed()
        self.assertEqual(staff.by_id(old.id), old)
        self.assertEqual(staff.by_id(old_unit.id), old_unit)
        self.assertIsNotNone(staff.by_path("good/models"))
        self.assertEqual(len([line for line in errors.splitlines() if "showrunner-bad" in line]), 1, errors)

    def test_showrunner_discovery_still_raises_unreadable_session_records(self) -> None:
        _ = self.production("sdlc")
        failure = OSError("session records are unreadable")
        with mock.patch.object(showrunners, "socket_for", side_effect=failure):
            with self.assertRaises(OSError) as raised:
                _ = showrunners.registered_showrunners()
        self.assertIs(raised.exception, failure)

    def test_a_stale_mac_retirement_keeps_a_unit_transferred_since_the_snapshot(self) -> None:
        old = staff.ensure("showrunner", name="old")
        new = staff.ensure("showrunner", name="new")
        unit = staff.ensure("unit", name="models", parent=old.id)
        evidence = staff.SeedInput((), (staff.SeedRetirement(unit.id, old.id),))
        moved = staff.move(unit.id, new.id)
        before = self.staff_file.read_bytes()
        with self.machine("mac"), redirect_stderr(io.StringIO()) as errors:
            record = staff.apply_seed(evidence)
        self.assertEqual(record.by_id(unit.id), moved)
        self.assertEqual(self.staff_file.read_bytes(), before)
        self.assertIn(unit.name, errors.getvalue())
        self.assertIn("parent changed", errors.getvalue())
        self.assertEqual(self.ssh_commands(), [["apply-seed", "--json"]])

    def test_retirement_transport_requires_the_observed_parent(self) -> None:
        parent = staff.ensure("showrunner", name="sdlc")
        unit = staff.ensure("unit", name="models", parent=parent.id)
        before = self.staff_file.read_bytes()
        for retirement in (unit.id, {"id": unit.id}, {"id": unit.id, "parent": None}):
            with self.subTest(retirement=retirement), self.assertRaises(staff.StaffRefused):
                _ = staff.apply_seed(staff.SeedInput.from_json({"members": [], "retire": [retirement]}))
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_only_a_typed_absent_record_bootstraps_seed(self) -> None:
        _ = self.production("sdlc")
        failure = staff.StaffUnreadable("cannot read staff record: [Errno 2] another read failed")
        with mock.patch.object(staff, "snapshot", side_effect=failure):
            with self.assertRaises(staff.StaffUnreadable) as raised:
                staff_sources.seed()
        self.assertIs(raised.exception, failure)
        self.assertFalse(self.staff_file.exists())

    def test_seed_uses_the_same_initial_record_read_as_the_writers(self) -> None:
        _ = self.production("sdlc")
        with mock.patch.object(staff, "initial_snapshot", wraps=staff.initial_snapshot) as initial:
            _ = self.seed()
        initial.assert_called_once_with()
        self.assertEqual([row.name for row in staff.snapshot().rows], ["sdlc"])


if __name__ == "__main__":
    _ = unittest.main()
