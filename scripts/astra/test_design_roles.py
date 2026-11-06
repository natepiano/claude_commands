"""Exercise durable design identity with local stores and a recording mesh transport."""
from __future__ import annotations

from scripts.production import staff, worker_admission
from scripts.production.staff_test_support import (
    fixture_member, fixture_runtime_id, fixture_unit_location, fixture_session,
    fixture_worker, showrunner_relationship, unit_membership,
)

from scripts.lib.signal_test_support import install_signal_pid_guard
from scripts.lib.test_models_registry import write_registry
from scripts.direct.test_implement_launcher import configure_pane_account

import argparse
import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast, override
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))
from scripts.astra import astra_roles  # noqa: E402
from scripts.accounts import role_map  # noqa: E402
from scripts.agents import codex_mesh, worker_lifecycle  # noqa: E402
from scripts.lib.code_kind import is_test_code  # noqa: E402
from scripts.astra import astra_work, design_roles  # noqa: E402
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.astra.design_role_test_support import write_empty_binding_record, write_mesh_server_record
from scripts.astra.design_roles import launch_thread as launch_native_role
from scripts.astra.design_roles import release_servers as release_design_servers  # noqa: E402
from scripts.whoami import agent_accounts  # noqa: E402
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.stop_injection_test_support import InjectedStop, stop_once

Role = Literal["designer", "reviewer"]


def damage_binding_repository(design: design_roles.HeldDesign) -> Path:
    """Plant the historical writer defect in an otherwise valid private record."""
    path = design_roles.design_directory(design.placement.design_id) / "design.json"
    fields = cast(dict[str, object], json.loads(path.read_text()))
    cast(dict[str, object], fields["binding"])["repository"] = ""
    _ = path.write_text(json.dumps(fields))
    return path


def setUpModule() -> None:
    install_signal_pid_guard()


class LiveDesignRoleNamesTests(unittest.TestCase):
    def test_live_modules_do_not_import_the_retired_package_or_read_its_environment(self) -> None:
        root = Path(__file__).resolve().parents[2]
        violations: list[str] = []
        for path in sorted((root / "scripts").rglob("*")):
            if not path.is_file() or is_test_code(path) or path.suffix not in (".py", ".sh"):
                continue
            relative = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".sh":
                violations.extend(f"{relative}:{number}: {line.strip()}"
                                  for number, line in enumerate(text.splitlines(), 1)
                                  if "GUILD_" in line or "scripts.guilds" in line)
                continue
            for node in ast.walk(ast.parse(text, filename=relative)):
                imports: tuple[str, ...] = ()
                if isinstance(node, ast.Import):
                    imports = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports = (node.module or "",)
                    if node.module == "scripts":
                        imports += tuple("scripts." + alias.name for alias in node.names)
                if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                        name == "scripts.guilds" or name.startswith("scripts.guilds.") for name in imports):
                    violations.append(f"{relative}:{node.lineno}: retired package import")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "GUILD_" in node.value or "scripts.guilds" in node.value:
                        violations.append(f"{relative}:{node.lineno}: retired name in a runtime string")
        self.assertEqual(violations, [], "\n".join(violations))


class DurableRolesTests(unittest.TestCase):
    root: Path = Path()
    repository: Path = Path()
    run_a: Path = Path()
    run_b: Path = Path()
    launches: list[tuple[str, str]] = []
    dispatches: list[tuple[str, str, str]] = []
    releases: list[str] = []
    holder_session: str = '17759e5d-1377-577b-b283-19888bbfc1f5'
    lose_creation_ack: bool = False
    fail_dispatch: bool = False
    pane_commands: list[list[str]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        from scripts.production.staff_test_support import StaffTestCase, production_membership, recorded_showrunner
        membership = StaffTestCase()
        membership.setUp()
        self.addCleanup(membership.doCleanups)
        document = membership.production("demo", "design")
        _ = recorded_showrunner("demo", document)
        production_membership("demo", "design")
        unit = unit_membership("demo", "design")
        _ = staff.update_current_session(unit.current_session, staff.native_id(self.holder_session))
        self.repository = self.root / "checkout"
        self.repository.mkdir()
        _ = document.write_text(document.read_text()
            + f"- **Merge branch:** main\n- **Showrunner checkout:** {self.repository}\n- **Log:** log.md\n")
        _ = (self.repository / "design.md").write_text("# Design\n")
        self.launches, self.dispatches, self.releases = [], [], []
        self.pane_commands = []
        self.holder_session, self.lose_creation_ack, self.fail_dispatch = '17759e5d-1377-577b-b283-19888bbfc1f5', False, False
        unit = staff.by_path("demo/design")
        assert unit is not None
        _ = staff.update_current_session(unit.current_session, staff.native_id(self.holder_session))
        _ = staff.set_worktree(unit.id, staff.PresentWorktree("natedev", self.repository), expected=unit.worktree)
        config = self.root / "models.conf"
        _ = write_registry(config, {'assignments': {'design_roles': 'codex'}, 'design_roles.codex': {'designer': 'gpt-fixture:xhigh', 'reviewer': 'gpt-review-fixture:high'}, 'codex.agents': {'gpt-fixture': 'xhigh', 'gpt-review-fixture': 'high'}})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root / "home"), "DESIGN_ROLES_STATE": str(self.root / "roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.root / "ownership"), "DESIGN_ROLES_HOST": "natedev",
            "ASTRA_ROLES_STATE": str(self.root / "holds"), "CODEX_THREAD_ID": self.holder_session,
            "USAGE_STATE_DIR": str(self.root / "usage"),
            "MODELS_CONFIG_FILE": str(config),
        })))
        _ = self.enterContext(patch.object(design_roles, "authority_request", side_effect=self.authority))
        _ = self.enterContext(patch.object(design_roles, "launch_thread", side_effect=self.launch))
        _ = self.enterContext(patch.object(design_roles, "dispatch_turn", side_effect=self.dispatch))
        _ = self.enterContext(patch.object(design_roles, "release_servers", side_effect=self.release))
        _ = self.enterContext(patch.object(role_map, "read_role_map", return_value=role_map.RoleMap({}, "codex1")))
        _ = self.enterContext(patch.object(agent_accounts, "codex_account_home", return_value=
            agent_accounts.CodexHomeHoldingLogin("codex1", self.root / "codex-home", "fixture@example.invalid")))
        design_roles.register_repository("fixture-repo", {"natedev": str(self.repository),
                                                    "macbook": str(self.root / "mac-checkout")})
        self.run_a = self.phase("run-a", "instance-a")
        self.run_b = self.phase("run-b", "instance-b")

    def test_herdr_role_start_keeps_its_creation_claim_when_pane_evidence_is_unknown(self) -> None:
        _ = (self.run_a / "worker_transport").write_text("herdr\n")
        notes = self.root / "notes"
        configure_pane_account(Path.home(), notes)
        parent = staff.by_path("demo/design")
        assert parent is not None
        _ = fixture_worker("impl", parent.id)
        before = Path(os.environ["STAFF_FILE"]).read_bytes()
        with patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr", "AGENT_NOTES_DIR": str(notes)}):
            with self.assertRaises(design_roles.RoleRefused) as refused:
                _ = self.open_designer()
        self.assertIn("/nonexistent/herdr", str(refused.exception))
        self.assertEqual(self.launches, [])
        self.assertEqual(self.dispatches, [])
        self.assertEqual(Path(os.environ["STAFF_FILE"]).read_bytes(), before)
        designs = list(design_roles.state_root().glob("*/design.json"))
        self.assertEqual(len(designs), 1)
        design = design_roles.read_design(designs[0].parent.name)
        lifecycle = design.roles[0].lifecycle
        self.assertIsInstance(lifecycle, design_roles.RoleNotOpened)
        assert isinstance(lifecycle, design_roles.RoleNotOpened)
        self.assertEqual(lifecycle.claim, "pane-start")

    def test_worker_state_accepts_holder_free_design_and_checks_its_structure(self) -> None:
        design = self.open_designer()
        path = design_roles.design_directory(design.placement.design_id) / "design.json"
        fields = cast(dict[str, object], json.loads(path.read_text()))
        self.assertNotIn("holder", fields)
        self.assertIs(worker_lifecycle.validate_worker_state(path, fields), fields)
        invalid: tuple[tuple[str, object], ...] = (
            ("placement", None), ("placement", []), ("binding", None), ("binding", []),
            ("roles", None), ("roles", [None]), ("roles", [{"lifecycle": None}]),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                with self.assertRaises(worker_lifecycle.WorkerStateUnreadable) as error:
                    _ = worker_lifecycle.validate_worker_state(path, {**fields, field: value})
                self.assertIn(str(path), str(error.exception))

    def test_open_rerun_after_claim_before_launch_finishes_one_thread(self) -> None:
        with stop_once(design_roles, "_mesh_context", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        retried = self.open_designer()
        self.assertIsInstance(self.instance(retried, "designer").lifecycle, design_roles.RoleOpen)
        self.assertEqual(len(self.launches), 1)

    def test_open_rerun_after_creation_preserves_the_thread_and_names_recovery(self) -> None:
        with stop_once(design_roles, "launch_thread"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        with redirect_stderr(io.StringIO()) as errors:
            retried = self.open_designer()
        self.assertIn("creation outcome unknown", errors.getvalue())
        self.assertEqual(len(self.launches), 1)
        lifecycle = self.instance(retried, "designer").lifecycle
        self.assertIsInstance(lifecycle, (design_roles.RoleNotOpened, design_roles.RoleOpen, design_roles.RoleNeedsRecovery))
        if isinstance(lifecycle, design_roles.RoleNotOpened):
            self.assertTrue(lifecycle.claim)
        folder = design_roles.mesh_directory(retried.placement.design_id)
        roster = cast(dict[str, object], json.loads((folder / "mesh_roster.json").read_text()))
        entry = cast(dict[str, object], roster[self.instance(retried, "designer").name])
        self.assertEqual(entry["staff_id"], self.launches[0][1])

    def test_rebind_rerun_after_generation_save_finishes_context_publication(self) -> None:
        design = self.open_designer()
        identifier = design.placement.design_id
        with stop_once(design_roles, "_save"), self.assertRaises(InjectedStop):
            _ = design_roles.rebind(identifier, expected_generation=1, reason="next phase",
                                    binding=self.binding(self.run_b))
        retried = design_roles.rebind(identifier, expected_generation=1, reason="next phase",
                                      binding=self.binding(self.run_b))
        self.assertEqual(retried.binding, self.binding(self.run_b))
        self.assertEqual(retried.placement.generation, 2)
        self.assertFalse((design_roles.design_directory(identifier) / "transition.json").exists())
        _ = self.resume(retried, "designer")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.dispatches[-1][1], self.launches[0][1])

    def test_rebind_rerun_with_current_binding_finishes_the_committed_generation(self) -> None:
        design = self.open_designer()
        identifier = design.placement.design_id
        path = self.repository / "next-design.md"
        _ = path.write_text("# Next design\n")
        document = str(path)
        with stop_once(design_roles, "_save"), self.assertRaises(InjectedStop):
            _ = design_roles.rebind(identifier, expected_generation=1, reason="next document", document=document)
        retried = design_roles.rebind(identifier, expected_generation=1, reason="next document", document=document)
        self.assertEqual(retried.binding, design.binding)
        self.assertEqual(retried.document, document)
        self.assertEqual(retried.placement.generation, 2)
        self.assertFalse((design_roles.design_directory(identifier) / "transition.json").exists())
        _ = self.resume(retried, "designer")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(self.dispatches[-1][1], self.launches[0][1])

    def test_end_rerun_after_ended_save_finishes_transition_and_census_cleanup(self) -> None:
        design = self.open_designer()
        identifier = design.placement.design_id
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)),
              patch.object(codex_mesh, "held_thread_end_confirmed", return_value=True)):
            with stop_once(design_roles, "_save"), self.assertRaises(InjectedStop):
                _ = design_roles.end(identifier + "/designer")
            ended = design_roles.end(identifier + "/designer")
        self.assertIsInstance(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded)
        self.assertFalse((design_roles.design_directory(identifier) / "transition.json").exists())
        self.assertFalse((self.root / "holds" / (self.launches[0][1] + ".json")).exists())

    def test_checkout_move_rerun_after_generation_save_finishes_routes(self) -> None:
        design = self.open_designer()
        destination = self.root / "moved-checkout"
        _ = self.repository.rename(destination)
        identifier = design.placement.design_id
        with stop_once(design_roles, "_save"), self.assertRaises(InjectedStop):
            design_roles.move_checkout(str(self.repository), str(destination), reason="checkout move")
        design_roles.move_checkout(str(self.repository), str(destination), reason="checkout move")
        moved = design_roles.read_design(identifier)
        self.assertEqual(moved.repository, str(destination))
        self.assertEqual(moved.placement.generation, 2)
        self.assertFalse((design_roles.design_directory(identifier) / "transition.json").exists())
        _ = self.resume(moved, "designer")
        self.assertEqual(len(self.launches), 1)

    def authority(self, operation: str, payload: dict[str, object]) -> dict[str, object]:
        return design_roles._authority_local(operation, payload)  # pyright: ignore[reportPrivateUsage]


    def phase(self, name: str, instance: str) -> Path:
        directory = self.root / name
        phase = directory / "phases/1"
        phase.mkdir(parents=True)
        owner = fixture_unit_location(showrunner_relationship("demo"), "design", self.repository)
        _ = (phase / "delegated_phase_reservation_state.json").write_text(json.dumps({
            "kind": "enrolled_awaiting_first_touch", "phase": "1"}))
        _ = (phase / "files").write_text("design.md\n")
        _ = (directory / "progress_history_state.json").write_text(json.dumps({
            "run_id": name, "owner_staff_id": owner.id, "history_file": str(self.root / (name + ".jsonl")),
            "phase": {"id": "1", "instance_id": instance, "status": "open"},
            "open_phases": [{"id": "1", "instance_id": instance, "status": "open"}],
        }))
        return directory

    def binding(self, directory: Path) -> design_roles.DesignBoundToPhase:
        return design_roles.DesignBoundToPhase(str(directory), "1", str(self.repository), ("design.md",))

    def instance(self, design: design_roles.HeldDesign, role: Role) -> design_roles.RoleInstance:
        return next(instance for instance in design.roles if instance.role == role)

    def launch(self, design: design_roles.HeldDesign, role: Role, prompt: Path, model: str, effort: str, *,
               admitted: worker_admission.WorkerStartAllowed) -> str:
        self.assertEqual(admitted.name, self.instance(design, role).name)
        self.assertTrue(prompt.is_file())
        self.assertEqual((model, effort), ("gpt-fixture", "xhigh") if role == "designer"
                         else ("gpt-review-fixture", "high"))
        label = "astra-" + role
        if any(opened_role == role for opened_role, _thread in self.launches):
            label += "-" + design.placement.design_id
        thread = fixture_runtime_id(label)
        mesh = design_roles.mesh_directory(design.placement.design_id)
        mesh.mkdir(parents=True, exist_ok=True)
        path = mesh / "mesh_roster.json"
        roster = cast(dict[str, object], json.loads(path.read_text())) if path.exists() else {}
        instance = self.instance(design, role)
        fixture_session(thread, staff.CodexBackend(self.root / "codex-home"))
        published = worker_admission.publish(admitted, thread)
        roster[instance.name] = {"staff_id": published.row.id, "status": "done",
                                 "held_design_id": design.placement.design_id, "held_role": role,
                                 "launcher_pid": 0}
        _ = path.write_text(json.dumps(roster))
        write_mesh_server_record(mesh, self.root / "codex-home")
        _ = worker_admission.publish(admitted, thread)
        self.launches.append((role, thread))
        if self.lose_creation_ack:
            raise TimeoutError("creation acknowledgement lost")
        return thread

    def dispatch(self, design: design_roles.HeldDesign, role: Role, prompt: Path, reply: Path,
                 model: str, effort: str) -> None:
        del model, effort
        lifecycle = self.instance(design, role).lifecycle
        self.assertIsInstance(lifecycle, (design_roles.RoleOpen, design_roles.RolePausedForMove))
        thread = cast(design_roles.RoleOpen, lifecycle).thread_id
        self.dispatches.append((role, thread, prompt.read_text()))
        if self.fail_dispatch:
            raise design_roles.RoleRefused("turn acceptance unknown")
        _ = reply.write_text("Saved reply from " + thread)

    def release(self, design: design_roles.HeldDesign) -> None:
        self.releases.append(design.placement.design_id)

    def open_designer(self, *, document: str = "design.md", request_id: str = "open-one") -> design_roles.HeldDesign:
        return design_roles.open_role("fixture-repo", document, "designer", "design", binding=self.binding(self.run_a), request_id=request_id)

    def resume(self, design: design_roles.HeldDesign, role: Role, text: str = "Revise with cited evidence.") -> Path:
        prompt = self.root / "assignment.md"
        _ = prompt.write_text(text)
        return design_roles.resume(design.placement.design_id + "/" + role, prompt)

    def pane_workers(self, *designs: design_roles.HeldDesign) -> PaneWorld:
        from scripts.production import worker

        world = PaneWorld()
        for index, instance in enumerate(instance for design in designs for instance in design.roles):
            if isinstance(instance.lifecycle, design_roles.RoleOpen):
                row = cast(staff.StaffRow, staff.by_id(instance.lifecycle.staff_id))
                world.add(row, f"w1:p{index}", family="codex")
        _ = self.enterContext(patch("herdr.Herdr", return_value=world))
        _ = self.enterContext(patch("herdr.native_process", side_effect=world.native_process))
        _ = self.enterContext(patch("herdr._startup_processes", side_effect=world.descendants))

        def command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            self.pane_commands.append(arguments.copy())
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                status = worker.main(arguments)
            return subprocess.CompletedProcess(arguments, status, output.getvalue(), errors.getvalue())

        _ = self.enterContext(patch.object(design_roles, "_worker_command", side_effect=command))
        return world

    def pane_start(self) -> PaneWorld:
        from scripts.accounts import accounts
        from scripts.lib import unit_runs
        from scripts.shutdown import restart

        for run in (self.run_a, self.run_b):
            _ = (run / "worker_transport").write_text("herdr\n")
        notes = self.root / "notes"
        configure_pane_account(Path.home(), notes)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"AGENT_NOTES_DIR": str(notes)})))
        _ = self.enterContext(patch.object(unit_runs, "RUN_ROOT", self.root / "unit-runs"))
        _ = self.enterContext(patch.object(unit_runs, "FORMER_RUN_ROOT", self.root / "former-runs"))
        world = self.pane_workers()
        parent = staff.by_path("demo/design")
        assert parent is not None
        world.add(parent, "w1:p0", family="codex")
        _ = self.enterContext(patch("herdr.launch_prepared_agent", side_effect=world.launch))
        _ = self.enterContext(patch.object(restart, "stop_row", side_effect=world.stop))
        _ = self.enterContext(patch.object(accounts, "role_account", return_value=
            accounts.CodexWorkersAccount("codex1", Path.home() / ".codex")))
        return world

    def test_pane_open_rerun_before_unknown_save_creates_one_worker(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_begin_creation", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        path, = design_roles.state_root().glob("*/design.json")
        saved = design_roles.read_design(path.parent.name)
        self.assertEqual(self.instance(saved, "designer").lifecycle,
                         design_roles.RoleNotOpened("pane-start", "not_started"))
        opened = self.open_designer()
        self.assertIsInstance(self.instance(opened, "designer").lifecycle, design_roles.RoleOpen)
        self.assertEqual(len(world.launches), 1)
        self.assertEqual([command[0] for command in self.pane_commands], ["add"])

    def test_pane_open_rerun_after_add_recovers_the_idle_worker_without_another_add(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_worker_command"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        with redirect_stderr(io.StringIO()) as errors:
            saved = self.open_designer()
        self.assertIn("creation outcome unknown", errors.getvalue())
        self.assertEqual(self.instance(saved, "designer").lifecycle,
                         design_roles.RoleNotOpened("pane-start", "outcome_unknown"))
        parent = staff.by_path("demo/design")
        assert parent is not None
        row = worker_admission.find_worker(staff.snapshot(), parent.id, self.instance(saved, "designer").name)
        assert row is not None
        recovered = design_roles.recover(saved.placement.design_id + "/designer")
        self.assertEqual(self.instance(recovered, "designer").lifecycle, design_roles.RoleOpen(row.id))
        self.assertEqual(self.open_designer(), recovered)
        self.assertEqual(len(world.launches), 1)
        self.assertEqual(sum(command[0] == "add" for command in self.pane_commands), 1)
        self.assertTrue((self.root / "holds" / (row.id + ".json")).exists())

    def test_pane_open_rerun_without_worker_evidence_retains_the_unknown_claim(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_worker_command", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        with redirect_stderr(io.StringIO()) as errors:
            saved = self.open_designer()
        self.assertIn("creation outcome unknown", errors.getvalue())
        with self.assertRaisesRegex(design_roles.RoleRefused, "no recorded worker; replacement refused"):
            _ = design_roles.recover(saved.placement.design_id + "/designer")
        self.assertEqual(design_roles.read_design(saved.placement.design_id), saved)
        self.assertEqual(self.instance(saved, "designer").lifecycle,
                         design_roles.RoleNotOpened("pane-start", "outcome_unknown"))
        self.assertEqual(self.pane_commands, [])
        self.assertEqual(world.launches, [])

    def test_pane_open_rerun_with_unknown_process_evidence_retains_the_claim(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_worker_command"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        world.unknown["w1:p1"] = "worker process evidence is unreadable"
        with redirect_stderr(io.StringIO()) as errors:
            saved = self.open_designer()
        self.assertIn("creation outcome unknown", errors.getvalue())
        with self.assertRaisesRegex(design_roles.RoleRefused, "process evidence is unavailable; worker is retained"):
            _ = design_roles.recover(saved.placement.design_id + "/designer")
        self.assertEqual(design_roles.read_design(saved.placement.design_id), saved)
        self.assertEqual(self.instance(saved, "designer").lifecycle,
                         design_roles.RoleNotOpened("pane-start", "outcome_unknown"))
        self.assertEqual(len(world.launches), 1)
        self.assertEqual(sum(command[0] == "add" for command in self.pane_commands), 1)

    def test_pane_end_clears_unknown_creation_before_the_worker_command(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_worker_command", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        path, = design_roles.state_root().glob("*/design.json")
        identifier = path.parent.name + "/designer"
        with self.assertRaises(design_roles.RoleRefused) as refused:
            _ = design_roles.recover(identifier)
        ended = design_roles.end(identifier)
        self.assertIsInstance(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded)
        self.assertEqual(design_roles.end(identifier), ended)
        self.assertIn("no recorded worker; replacement refused", str(refused.exception))
        self.assertIn("end", str(refused.exception))
        self.assertFalse((path.parent / "transition.json").exists())
        self.assertEqual(self.pane_commands, [["end", self.instance(ended, "designer").name]])
        self.assertEqual(world.launches, [])

    def test_mesh_end_clears_unknown_creation_before_the_worker_command(self) -> None:
        with stop_once(design_roles, "launch_thread", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        path, = design_roles.state_root().glob("*/design.json")
        identifier = path.parent.name + "/designer"
        with self.assertRaises(design_roles.RoleRefused) as refused:
            _ = design_roles.recover(identifier)
        ended = design_roles.end(identifier)
        self.assertIsInstance(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded)
        self.assertEqual(design_roles.end(identifier), ended)
        self.assertIn("no recorded thread; replacement refused", str(refused.exception))
        self.assertIn("end", str(refused.exception))
        self.assertFalse((path.parent / "transition.json").exists())
        self.assertEqual(self.launches, [])

    def test_end_unknown_creation_refuses_another_roles_creation_claim(self) -> None:
        with stop_once(design_roles, "launch_thread", when="before"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        with stop_once(design_roles, "launch_thread", when="before"), self.assertRaises(InjectedStop):
            _ = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design",
                                       binding=self.binding(self.run_a))
        path, = design_roles.state_root().glob("*/design.json")
        saved = design_roles.read_design(path.parent.name)
        with self.assertRaisesRegex(design_roles.RoleRefused, "role is busy; an assignment claim is active"):
            _ = design_roles.end(path.parent.name + "/designer")
        self.assertEqual(design_roles.read_design(path.parent.name), saved)
        self.assertFalse((path.parent / "transition.json").exists())
        self.assertEqual(self.launches, [])

    def test_pane_end_unknown_creation_finishes_the_existing_worker_cleanup(self) -> None:
        world = self.pane_start()
        with stop_once(design_roles, "_worker_command"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        path, = design_roles.state_root().glob("*/design.json")
        saved = design_roles.read_design(path.parent.name)
        parent = staff.by_path("demo/design")
        assert parent is not None
        row = worker_admission.find_worker(staff.snapshot(), parent.id, self.instance(saved, "designer").name)
        assert row is not None
        _ = world.foreground.pop("w1:p1")
        with patch.object(world, "close_tab_confirmed", return_value=False):
            with self.assertRaisesRegex(design_roles.RoleRefused, "pane close is incomplete"):
                _ = design_roles.end(path.parent.name + "/designer")
        self.assertEqual(design_roles.read_design(path.parent.name), saved)
        self.assertIsNotNone(staff.by_id(row.id))
        ended = design_roles.end(path.parent.name + "/designer")
        self.assertEqual(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded(row.id))
        self.assertEqual(design_roles.end(path.parent.name + "/designer"), ended)
        self.assertIsNone(staff.by_id(row.id))
        self.assertFalse((path.parent / "transition.json").exists())
        self.assertEqual(len(world.launches), 1)

    def test_mesh_end_unknown_creation_finishes_the_existing_worker_cleanup(self) -> None:
        with stop_once(design_roles, "launch_thread"), self.assertRaises(InjectedStop):
            _ = self.open_designer()
        path, = design_roles.state_root().glob("*/design.json")
        saved = design_roles.read_design(path.parent.name)
        identifier = path.parent.name + "/designer"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stderr="cleanup refused")):
            with self.assertRaisesRegex(design_roles.RoleRefused, "cleanup refused"):
                _ = design_roles.end(identifier)
        self.assertEqual(design_roles.read_design(path.parent.name), saved)
        row = staff.by_current_session(self.launches[0][1])
        assert row is not None
        with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)),
              patch.object(codex_mesh, "held_thread_end_confirmed", return_value=True)):
            ended = design_roles.end(identifier)
        self.assertEqual(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded(row.id))
        self.assertEqual(design_roles.end(identifier), ended)
        self.assertIsNone(staff.by_id(row.id))
        self.assertFalse((path.parent / "transition.json").exists())
        self.assertEqual(len(self.launches), 1)

    def test_pane_rebind_rerun_before_transition_clear_finishes_publication(self) -> None:
        world = self.pane_start()
        design = self.open_designer()
        identifier = design.placement.design_id
        transition = design_roles.design_directory(identifier) / "transition.json"
        document = self.repository / "next-design.md"
        _ = document.write_text("# Next design\n")

        def rebind() -> design_roles.HeldDesign:
            return design_roles.rebind(identifier, expected_generation=1,
                                       reason="next document", document=str(document))

        with (stop_once(Path, "unlink", when="before", matches=lambda path: path == transition),
              self.assertRaises(InjectedStop)):
            _ = rebind()
        self.assertTrue(transition.exists())
        saved = design_roles.read_design(identifier)
        rebound = rebind()
        self.assertEqual(rebound, saved)
        self.assertEqual(rebound.document, str(document))
        self.assertEqual(rebound.placement.generation, 2)
        self.assertEqual(rebound.roles, design.roles)
        self.assertFalse(transition.exists())
        context = cast(dict[str, object], json.loads((design_roles.mesh_directory(identifier) / "design_context.json").read_text()))
        self.assertEqual(context["generation"], 2)
        self.assertEqual(len(world.launches), 1)

    def test_pane_end_rerun_after_transport_failure_keeps_then_finishes_its_transition(self) -> None:
        world = self.pane_start()
        design = self.open_designer()
        _ = world.foreground.pop("w1:p1")
        identifier = design.placement.design_id
        transition = design_roles.design_directory(identifier) / "transition.json"
        lifecycle = self.instance(design, "designer").lifecycle
        assert isinstance(lifecycle, design_roles.RoleOpen)
        with patch.object(world, "close_tab_confirmed", return_value=False):
            with self.assertRaisesRegex(design_roles.RoleRefused, "pane close is incomplete"):
                _ = design_roles.end(identifier + "/designer")
        self.assertTrue(transition.exists())
        self.assertEqual(design_roles.read_design(identifier), design)
        self.assertIsNotNone(staff.by_id(lifecycle.staff_id))
        ended = design_roles.end(identifier + "/designer")
        self.assertEqual(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded(lifecycle.staff_id))
        self.assertFalse(transition.exists())
        self.assertIsNone(staff.by_id(lifecycle.staff_id))
        self.assertFalse((self.root / "holds" / (lifecycle.staff_id + ".json")).exists())

    def test_pane_end_rerun_after_terminal_save_finishes_census_and_staff_cleanup(self) -> None:
        world = self.pane_start()
        design = self.open_designer()
        _ = world.foreground.pop("w1:p1")
        identifier = design.placement.design_id
        lifecycle = self.instance(design, "designer").lifecycle
        assert isinstance(lifecycle, design_roles.RoleOpen)
        transition = design_roles.design_directory(identifier) / "transition.json"
        census = self.root / "holds" / (lifecycle.staff_id + ".json")
        with stop_once(design_roles, "_retire_role", when="before"), self.assertRaises(InjectedStop):
            _ = design_roles.end(identifier + "/designer")
        self.assertEqual(self.instance(design_roles.read_design(identifier), "designer").lifecycle,
                         design_roles.RoleEnded(lifecycle.staff_id))
        self.assertTrue(transition.exists())
        self.assertTrue(census.exists())
        ended = design_roles.end(identifier + "/designer")
        self.assertEqual(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded(lifecycle.staff_id))
        self.assertFalse(transition.exists())
        self.assertFalse(census.exists())
        self.assertIsNone(staff.by_id(lifecycle.staff_id))
        self.assertEqual(sum(command[0] == "end" for command in self.pane_commands), 1)
        self.assertEqual(len(world.closed), 1)

    def test_pane_checkout_move_rerun_before_transition_clear_keeps_generation_and_thread(self) -> None:
        world = self.pane_start()
        design = self.open_designer()
        identifier = design.placement.design_id
        transition = design_roles.design_directory(identifier) / "transition.json"
        destination = self.root / "moved-checkout"
        _ = self.repository.rename(destination)

        def move() -> None:
            design_roles.move_checkout(str(self.repository), str(destination), reason="checkout move")

        with (stop_once(Path, "unlink", when="before", matches=lambda path: path == transition),
              self.assertRaises(InjectedStop)):
            move()
        saved = design_roles.read_design(identifier)
        self.assertTrue(transition.exists())
        move()
        moved = design_roles.read_design(identifier)
        self.assertEqual(moved, saved)
        self.assertEqual(moved.repository, str(destination))
        self.assertEqual(moved.placement.generation, 2)
        self.assertEqual(moved.roles, design.roles)
        self.assertFalse(transition.exists())
        context = cast(dict[str, object], json.loads((design_roles.mesh_directory(identifier) / "design_context.json").read_text()))
        self.assertEqual(context["cwd"], str(destination))
        self.assertEqual(len(world.launches), 1)

    def test_sibling_mesh_activity_or_claim_blocks_dispatch_and_keeps_the_binding(self) -> None:
        design = self.open_designer()
        design = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design",
                                        binding=self.binding(self.run_a))
        roster = design_roles.mesh_directory(design.placement.design_id) / "mesh_roster.json"
        entries = cast(dict[str, dict[str, object]], json.loads(roster.read_text()))
        path = design_roles.design_directory(design.placement.design_id) / "design.json"
        for transport in ("mesh", "herdr"):
            _ = (self.run_a / "worker_transport").write_text(transport + "\n")
            for status, claim in (("running", ""), ("done", "review-in-progress")):
                with self.subTest(transport=transport, status=status, claim=claim):
                    entries[self.instance(design, "reviewer").name]["status"] = status
                    _ = roster.write_text(json.dumps(entries))
                    fields = cast(dict[str, object], json.loads(path.read_text()))
                    roles = cast(list[dict[str, object]], fields["roles"])
                    roles[1]["assignment_claim"] = claim
                    _ = path.write_text(json.dumps(fields))
                    before = design_roles.read_design(design.placement.design_id)
                    with self.assertRaisesRegex(design_roles.RoleRefused, "role is busy"):
                        _ = self.resume(before, "designer")
                    self.assertEqual(design_roles.read_design(design.placement.design_id), before)
                    self.assertEqual(self.dispatches, [])

    def test_busy_sibling_does_not_stop_selected_role_dispatch(self) -> None:
        design = self.open_designer()
        design = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design",
                                        binding=self.binding(self.run_a))
        world = self.pane_workers(design)
        world.locations["w1:p1"]["agent_status"] = "working"
        roster = design_roles.mesh_directory(design.placement.design_id) / "mesh_roster.json"
        _ = roster.write_text("{}")
        _ = (self.run_a / "worker_transport").write_text("herdr\n")
        reply = self.resume(design, "designer")
        self.assertEqual(reply.read_text(), "Saved reply from " + self.launches[0][1])
        current = design_roles.read_design(design.placement.design_id)
        self.assertEqual(self.instance(current, "reviewer"), self.instance(design, "reviewer"))
        self.assertEqual([role for role, _, _ in self.dispatches], ["designer"])

    def test_unknown_sibling_does_not_stop_selected_role_rebind(self) -> None:
        design = self.open_designer()
        design = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design",
                                        binding=self.binding(self.run_a))
        world = self.pane_workers(design)
        world.unknown["w1:p1"] = "sibling process evidence is unreadable"
        roster = design_roles.mesh_directory(design.placement.design_id) / "mesh_roster.json"
        _ = roster.write_text("{}")
        _ = (self.run_a / "worker_transport").write_text("herdr\n")
        rebound = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                      reason="next phase", binding=self.binding(self.run_b))
        self.assertEqual(design_roles.read_design(design.placement.design_id).binding, self.binding(self.run_b))
        self.assertEqual(rebound.roles, design.roles)
        self.assertEqual(world.stops, [])
        self.assertEqual(world.closed, [])

    def test_astra_role_publishes_the_returned_thread_as_its_staff_identity(self) -> None:
        self.check_native_role("demo/design")

    def test_showrunner_held_astra_role_keeps_its_worker_row_and_backend(self) -> None:
        self.check_native_role("demo")

    def check_native_role(self, parent_path: str) -> None:
        from scripts.agents import test_codex_mesh as mesh_fixture
        from scripts.production.staff_test_support import production_membership, recorded_showrunner

        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "codex-home"),
            "AGENT_NOTES_DIR": str(self.root / "agent-notes"),
        })))
        owner = staff.by_path("demo")
        assert owner is not None and isinstance(owner.doc, staff.LiveDoc)
        document = owner.doc.path
        backend = mesh_fixture.MeshCommandFixture()
        backend.setUp()
        self.addCleanup(backend.doCleanups)
        self.addCleanup(backend.tearDown)
        _ = recorded_showrunner("demo", document)
        production_membership("demo", "design")
        _ = fixture_unit_location(showrunner_relationship("demo"), "design", self.repository)
        parent = staff.by_path(parent_path)
        assert parent is not None
        os.environ["CODEX_THREAD_ID"] = parent.current_session
        self.holder_session = parent.current_session
        design_roles.register_repository("fixture-repo", {"natedev": str(self.repository),
                                                        "macbook": str(self.root / "mac-checkout")})
        commands: list[list[str]] = []

        def prime(arguments: list[str], *, capture_output: bool, text: bool,
                  check: bool, timeout: float | None = None,
                  env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if arguments[1] != str(design_roles.ROOT / "scripts/direct/assign.sh"):
                self.assertTrue(text)
                return subprocess.run(arguments, capture_output=capture_output, text=True,
                                      check=check, timeout=timeout, env=env)
            self.assertTrue(capture_output and text)
            self.assertFalse(check)
            self.assertEqual(arguments[:6], ["bash", str(design_roles.ROOT / "scripts/direct/assign.sh"),
                                           "--role", "implement", "--operation", "prime"])
            self.assertNotIn("--sandbox", arguments)
            self.assertNotIn("--paths", arguments)
            commands.append(arguments)
            mesh = Path(arguments[arguments.index("--session-dir") + 1])
            mesh.mkdir(parents=True, exist_ok=True)
            server = cast(dict[str, object], json.loads((backend.session_dir / "mesh_server.json").read_text()))
            server["codex_home"] = str(self.root / "codex-home")
            server["design_context"] = codex_mesh._design_context(str(mesh))  # pyright: ignore[reportPrivateUsage]
            _ = (mesh / "mesh_server.json").write_text(json.dumps(server))
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                options = dict(zip(arguments[6::2], arguments[7::2]))
                self.assertEqual(options["--held-role"], "designer")
                code = codex_mesh.main([
                    "prime", "--session-dir", str(mesh), "--name", options["--new-worker"],
                    "--cwd", options["--cwd"], "--instructions-file", options["--prompt"],
                    "--model", options["--model"], "--effort", options["--effort"],
                    "--sandbox", "danger-full-access", "--held-design", options["--held-design"],
                    "--held-role", options["--held-role"],
                ])
            return subprocess.CompletedProcess(arguments, code, output.getvalue(), errors.getvalue())

        signal_process = os.kill

        def check_signal(pid: int, signum: int) -> None:
            if signum != 0:
                raise AssertionError(f"fixture server must be reused, not signalled: {pid}, {signum}")
            signal_process(pid, signum)

        with patch.object(design_roles, "launch_thread", side_effect=launch_native_role), \
                patch.object(design_roles, "subprocess", SimpleNamespace(run=prime)), \
                patch.object(os, "kill", side_effect=check_signal):
            design = design_roles.open_role("fixture-repo", "design.md", "designer", "design", binding=self.binding(self.run_a), request_id="open-one")
        instance = self.instance(design, "designer")
        self.assertIsInstance(instance.lifecycle, design_roles.RoleOpen)
        assert isinstance(instance.lifecycle, design_roles.RoleOpen)
        row = staff.by_id(instance.lifecycle.staff_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual((row.id, row.name), (mesh_fixture.THREAD_ID, instance.name))
        self.assertEqual(row.name, instance.name)
        parent = staff.by_path(parent_path)
        assert parent is not None
        self.assertEqual(row.parent, staff.ReportsTo(parent.id))
        self.assertEqual(staff.snapshot().backends[row.id], staff.CodexBackend(self.root / "codex-home"))
        self.assertEqual(len(commands), 1)
        self.assertEqual(len(backend.methods("thread/start")), 1)
        self.assertEqual(backend.methods("turn/start"), [])
        self.assertEqual(self.dispatches, [])

    def test_a_phase_binding_without_a_run_owner_names_the_run_directory(self) -> None:
        from scripts.direct import phase_reservation

        path = self.run_a / "progress_history_state.json"
        state = cast(dict[str, object], json.loads(path.read_text()))
        _ = state.pop("owner_staff_id")
        _ = path.write_text(json.dumps(state))
        for contents in (None, "", "\n"):
            with self.subTest(director=contents):
                if contents is not None:
                    _ = (self.run_a / "director").write_text(contents)
                with self.assertRaises(phase_reservation.StateRefused) as refusal:
                    _ = self.open_designer()
                self.assertEqual(str(refusal.exception), f"{self.run_a}: run owner is unavailable")
                self.assertEqual(self.launches, [])

    def test_staff_refusal_does_not_claim_or_launch_an_astra_role(self) -> None:
        with patch.object(worker_admission, "admit", side_effect=worker_admission.AdmissionRefused("staff: demo-design-astra-designer: not opened: write refused")):
            with self.assertRaisesRegex(ValueError, "staff: demo-design-astra-designer: not opened: write refused"):
                _ = self.open_designer()
        self.assertEqual(self.launches, [])
        self.assertTrue(all(isinstance(role.lifecycle, design_roles.RoleNotOpened) and not role.lifecycle.claim
                            for path in design_roles.state_root().glob("*/design.json")
                            for role in design_roles.read_design(path.parent.name).roles))

    def test_damaged_design_roster_refuses_publication_without_erasing_ownership(self) -> None:
        design = self.open_designer()
        path = design_roles.mesh_directory(design.placement.design_id) / "mesh_roster.json"
        for damaged in ("", '{"designer":', "not JSON", "[]", '{"designer": []}'):
            with self.subTest(damaged=damaged):
                _ = path.write_text(damaged)
                with self.assertRaises(Exception) as refused:
                    with design_roles._edit_roster(design.placement.design_id) as roster:  # pyright: ignore[reportPrivateUsage]
                        roster["replacement"] = {"thread_id": "fresh", "status": "done"}
                self.assertEqual(type(refused.exception).__name__, "WorkerStateUnreadable")
                self.assertIn(str(path), str(refused.exception))
                self.assertEqual(path.read_text(), damaged)
                self.assertEqual(design_roles.read_design(design.placement.design_id), design)
        self.assertEqual(len(self.launches), 1)

    def test_design_ownership_read_names_damaged_state_instead_of_decoding_it_as_empty(self) -> None:
        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / "design.json"
        for damaged in ("", '{"placement":', "not JSON", "[]", '{"roles": [42]}'):
            with self.subTest(damaged=damaged):
                _ = path.write_text(damaged)
                with self.assertRaises(Exception) as refused:
                    _ = design_roles.read_design(design.placement.design_id)
                self.assertEqual(type(refused.exception).__name__, "WorkerStateUnreadable")
                self.assertIn(str(path), str(refused.exception))
                self.assertEqual(path.read_text(), damaged)

    def test_old_repository_record_opens_and_retries_keep_one_pair_and_thread(self) -> None:
        store = self.root / "ownership/repositories.json"
        record = {"repository_id": "fixture-repo", "checkouts": {"natedev": str(self.repository)}}
        _ = store.write_text(json.dumps({"fixture-repo": record}))
        design = self.open_designer()
        for complete in (False, True):
            _ = store.write_text(json.dumps({"fixture-repo": {**record, "inventory_complete": complete}}))
            self.assertEqual(self.open_designer(), design)
        self.assertIn('"inventory_complete": true', store.read_text())
        retried = self.open_designer(document=str(self.repository / "sub/../design.md"), request_id="retry")
        self.assertEqual(retried.placement.design_id, design.placement.design_id)
        self.assertEqual(self.launches, [("designer", "9309c903-5894-501a-8aa7-f33ea912e074")])
        self.assertEqual(len(list((self.root / "ownership/placements").glob("*.json"))), 1)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_cli_open_without_checkout_saves_the_resolved_binding_repository(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = design_roles.main(['open', '--repository', 'fixture-repo', '--document', 'design.md',
                '--role', 'designer', '--area', 'design',
                '--run-directory', str(self.run_a), '--phase', '1', '--output-path', 'design.md'])
        self.assertEqual(code, 0, output.getvalue())
        design_id = output.getvalue().strip().removesuffix('/designer')
        design = design_roles.read_design(design_id)
        self.assertEqual(design.binding, self.binding(self.run_a))
        self.assertEqual(self.instance(design, 'designer').lifecycle, design_roles.RoleOpen(staff.StaffId('9309c903-5894-501a-8aa7-f33ea912e074')))

    def test_save_refuses_an_empty_binding_repository_without_changing_the_record(self) -> None:
        design = self.open_designer()
        path = design_roles.design_directory(design.placement.design_id) / 'design.json'
        before = path.read_bytes()
        with self.assertRaisesRegex(design_roles.RoleRefused, 'repository'):
            design_roles._save(replace(design, binding=replace(self.binding(self.run_a), repository='')))  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(path.read_bytes(), before)

    def test_sweeps_skip_an_unrelated_bad_binding_and_keep_processing_valid_roles(self) -> None:

        bad = write_empty_binding_record(self.repository)
        design = self.open_designer()
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(design_roles.held_document(Path(design.document)), design)
            design_roles.unbind_run(str(self.run_a))
        unbound = design_roles.read_design(design.placement.design_id)
        self.assertIsInstance(unbound.binding, design_roles.DesignUnbound)
        self.assertEqual(unbound.roles, design.roles)
        self.assertEqual(errors.getvalue().splitlines(), [f'warning: {bad}: missing or invalid repository'] * 2)
        self.assertTrue(bad.is_file())


    def test_sweeps_refuse_a_matching_bad_record_with_a_live_role(self) -> None:

        operations: tuple[Callable[[], object], ...] = (
            lambda: design_roles.unbind_run(str(self.run_a)),
            lambda: design_roles.held_document(self.repository / 'broken-design.md'))
        for lifecycle in (design_roles.RoleOpen(staff.StaffId('held-thread')), design_roles.RolePausedForMove(staff.StaffId('held-thread')),
                          design_roles.RoleNeedsRecovery(staff.StaffId('held-thread'), 'lost acknowledgement')):
            for operation in operations:
                with self.subTest(lifecycle=lifecycle, operation=operation):
                    bad = write_empty_binding_record(self.repository, lifecycle=lifecycle,
                                                     run_directory=str(self.run_a))
                    before = bad.read_bytes()
                    with self.assertRaisesRegex(design_roles.RoleRefused, 'live held role needs recovery'):
                        _ = operation()
                    self.assertEqual(bad.read_bytes(), before)
                    bad.unlink()

    def test_run_cleanup_skips_another_runs_damaged_record_and_a_retry_writes_nothing(self) -> None:
        design = self.open_designer()
        bad = write_empty_binding_record(self.repository,
                                        lifecycle=design_roles.RoleOpen(staff.StaffId('other-thread')), run_directory=str(self.run_b))
        for attempt in range(2):
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.root.rglob('*.json')}
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = design_roles.main(['unbind-run', str(self.run_a)])
            self.assertEqual(code, 0, error.getvalue())
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(error.getvalue().splitlines(), [f'warning: {bad}: missing or invalid repository'])
            current = design_roles.read_design(design.placement.design_id)
            self.assertIsInstance(current.binding, design_roles.DesignUnbound)
            self.assertEqual(current.placement.generation, 2)
            self.assertEqual(current.roles, design.roles)
            self.assertEqual((bad.read_bytes(), bad.stat().st_mtime_ns), before[bad])
            if attempt:
                self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in self.root.rglob('*.json')}, before)
        self.assertEqual(self.releases, [design.placement.design_id])

    def test_run_cleanup_refuses_matching_damage_without_a_live_role(self) -> None:
        for lifecycle in (design_roles.RoleNotOpened(), design_roles.RoleEnded(staff.StaffId('ended-thread'))):
            with self.subTest(lifecycle=lifecycle):
                bad = write_empty_binding_record(self.repository,
                                                lifecycle=lifecycle, run_directory=str(self.run_a))
                before = bad.read_bytes(), bad.stat().st_mtime_ns
                error = io.StringIO()
                with redirect_stderr(error):
                    code = design_roles.main(['unbind-run', str(self.run_a)])
                self.assertEqual(code, 2)
                self.assertEqual(error.getvalue().splitlines(), [f'design_roles: {bad}: missing or invalid repository'])
                self.assertEqual((bad.read_bytes(), bad.stat().st_mtime_ns), before)
                bad.unlink()
        self.assertEqual(self.releases, [])

    def test_document_lookup_warns_once_for_invalid_json_and_keeps_the_valid_design(self) -> None:
        design = self.open_designer()
        bad = write_empty_binding_record(self.repository)
        _ = bad.write_text('{"holder":\n')
        errors = io.StringIO()
        with redirect_stderr(errors):
            found = design_roles.held_document(Path(design.document))
        self.assertEqual(found, design)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(f'warning: {bad}: ', errors.getvalue())

    def test_holder_checks_skip_bad_unrelated_records_and_keep_valid_failures(self) -> None:

        bad = write_empty_binding_record(self.repository, lifecycle=design_roles.RoleOpen(staff.StaffId('unrelated-thread')))
        design = self.open_designer()
        report = design_roles.list_roles()
        entries = cast(list[dict[str, object]], report['roles'])
        self.assertEqual(len(entries), 2)
        self.assertEqual({entry['design_id'] for entry in entries}, {design.placement.design_id})
        self.assertIn(str(bad), str(report['problems']))
        self.assertTrue(all('holder' not in entry and 'holder_reach' not in entry for entry in entries))


    def test_holder_checks_refuse_only_matching_bad_live_roles(self) -> None:

        for lifecycle in (design_roles.RoleNotOpened(), design_roles.RoleEnded(staff.StaffId('ended-thread')),
                          design_roles.RoleOpen(staff.StaffId('held-thread')), design_roles.RolePausedForMove(staff.StaffId('held-thread')),
                          design_roles.RoleNeedsRecovery(staff.StaffId('held-thread'), 'lost acknowledgement')):
            with self.subTest(lifecycle=lifecycle):
                bad = write_empty_binding_record(self.repository, lifecycle=lifecycle)
                before = bad.read_bytes()
                report = design_roles.list_roles()
                self.assertIn(str(bad), str(report['problems']))
                self.assertEqual(bad.read_bytes(), before)
                bad.unlink()


    def test_record_operation_warns_once_across_holders_and_warns_again_on_retry(self) -> None:

        bad = write_empty_binding_record(self.repository)
        design = self.open_designer()
        for _attempt in range(2):
            errors = io.StringIO()
            with redirect_stderr(errors), design_roles.record_operation():
                with design_roles.record_operation():
                    for _ in range(3):
                        self.assertEqual(design_roles.held_document(Path(design.document)), design)
            self.assertEqual(errors.getvalue().splitlines(), [f'warning: {bad}: missing or invalid repository'])
        self.assertEqual(design_roles.read_design(design.placement.design_id), design)


    def test_unregistered_repository_refusal_names_the_register_command(self) -> None:
        store = self.root / "ownership/repositories.json"
        for contents in (None, "{}"):
            if contents is None:
                store.unlink()
            else:
                _ = store.write_text(contents)
            for operation in ("open", "resolve", "checkout"):
                for host in ("natedev", "macbook", ""):
                    if operation == "open" and host != "natedev":
                        continue
                    with self.subTest(contents=contents, operation=operation, host=host):
                        payload: dict[str, object] = {
                            "repository_id": "fixture-repo", "document": "design.md",
                            "old_checkout": str(self.repository), "new_checkout": str(self.root / "new-checkout"),
                        }
                        if host:
                            payload["host"] = host
                        with self.assertRaises(design_roles.RoleRefused) as refused:
                            if operation == "open":
                                _ = self.open_designer()
                            else:
                                _ = self.authority(operation, payload)
                        self.assertEqual(str(refused.exception),
                                         "repository fixture-repo is not registered; run design_roles.py register "
                                         + f"--repository fixture-repo --host {host or '<host>'} "
                                         + "--checkout <absolute path>")
        self.assertEqual(self.launches, [])

    def test_malformed_repository_record_keeps_the_object_refusal(self) -> None:
        store = self.root / "ownership/repositories.json"
        records: tuple[object, ...] = ([], "invalid", 42, False)
        for record in records:
            _ = store.write_text(json.dumps({"fixture-repo": record}))
            for operation in ("reserve", "resolve", "checkout"):
                with self.subTest(record=record, operation=operation):
                    with self.assertRaises(design_roles.RoleRefused) as refused:
                        _ = self.authority(operation, {"repository_id": "fixture-repo", "host": "natedev"})
                    self.assertEqual(str(refused.exception), "expected a JSON object")

    def test_registered_document_alias_reuses_the_original_pair(self) -> None:
        design = self.open_designer()
        _ = self.authority("alias", {"repository_id": "fixture-repo", "host": "natedev",
                                     "document": "design.md", "alias": "renamed/design.md"})
        retried = self.open_designer(document="renamed/design.md")
        self.assertEqual(retried.placement.design_id, design.placement.design_id)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_unknown_thread_creation_retains_the_claim_instead_of_launching_again(self) -> None:
        with patch.object(design_roles, "launch_thread", side_effect=ConnectionError("creation outcome unknown")) as launch:
            with self.assertRaises(ConnectionError):
                _ = self.open_designer()
            retry = self.open_designer(request_id="another-attempt")
        self.assertEqual(launch.call_count, 1)
        lifecycle = self.instance(retry, "designer").lifecycle
        self.assertIsInstance(lifecycle, design_roles.RoleNotOpened)
        if isinstance(lifecycle, design_roles.RoleNotOpened):
            self.assertTrue(lifecycle.claim)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_simultaneous_hosts_reserve_only_one_document_placement(self) -> None:
        def reserve(host: str) -> str:
            try:
                placement = self.authority("reserve", {"repository_id": "fixture-repo", "document": "race.md",
                                                       "host": host, "request_id": host})
                return str(placement["design_id"])
            except design_roles.RoleRefused:
                return "refused"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ("natedev", "macbook")))
        self.assertEqual(results.count("refused"), 1)
        self.assertEqual(len(list((self.root / "ownership/placements").glob("*.json"))), 1)
        self.assertEqual(self.launches, [])

    def test_lost_creation_acknowledgement_never_launches_a_second_thread(self) -> None:
        self.lose_creation_ack = True
        with self.assertRaises(TimeoutError):
            _ = self.open_designer()
        retried = self.open_designer()
        lifecycle = self.instance(retried, "designer").lifecycle
        self.assertIsInstance(lifecycle, (design_roles.RoleNotOpened, design_roles.RoleNeedsRecovery))
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(list((self.root / "roles").glob("*/design.json"))), 1)

    def test_unreachable_authority_and_host_mismatch_cannot_create_roles(self) -> None:
        with patch.object(design_roles, "authority_request", side_effect=design_roles.RoleRefused("authority unavailable")):
            with self.assertRaisesRegex(design_roles.RoleRefused, "authority unavailable"):
                _ = self.open_designer()
        with self.assertRaisesRegex(design_roles.RoleRefused, "host"):
            _ = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design", host="macbook")
        self.assertEqual(self.launches, [])
        self.assertEqual(list((self.root / "roles").glob("*/design.json")), [])

    def test_invalid_model_refuses_before_thread_creation(self) -> None:
        with patch.object(design_roles, "resolve_model", side_effect=design_roles.RoleRefused("invalid design role model")), \
                self.assertRaises(ValueError):
            _ = self.open_designer()
        self.assertEqual(self.launches, [])

    def test_missing_or_disallowed_configured_model_never_uses_a_fallback(self) -> None:
        config = Path(os.environ["MODELS_CONFIG_FILE"])
        for model in (None, "unlisted-model:xhigh", "gpt-fixture:invalid-effort"):
            with self.subTest(model=model):
                _ = write_registry(config, {"design_roles.codex": {"designer": model},
                                            "codex.agents": {"gpt-fixture": "xhigh"}})
                with self.assertRaisesRegex(design_roles.RoleRefused, "model configuration"):
                    _ = self.open_designer()
        self.assertEqual(self.launches, [])

    def test_showrunner_cannot_open_unbound_writer_but_reviewer_is_read_only(self) -> None:
        with self.assertRaisesRegex(design_roles.RoleRefused, "binding"):
            _ = design_roles.open_role("fixture-repo", "design.md", "designer", "design")
        design = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design")
        self.assertIsInstance(design_roles.access_policy(design, "reviewer"), design_roles.ReadOnlyReviewer)
        self.assertEqual(self.launches, [("reviewer", "364898ad-f177-5514-80f3-b82001fa95d9")])

    def test_prompts_and_replies_are_saved_with_phase_assignment_headers(self) -> None:
        design = self.open_designer()
        reply = self.resume(design, "designer")
        prompts = sorted(design_roles.design_directory(design.placement.design_id).glob("prompts/*.md"))
        self.assertEqual(len(prompts), 2)
        header = astra_work.read_header(prompts[-1])
        self.assertIsInstance(header, astra_work.AstraWork)
        if not isinstance(header, astra_work.AstraWork):
            self.fail("saved assignment has no work header")
        self.assertEqual((header.member, header.role, header.reply_path),
                         (self.instance(design, "designer").name, "astra-designer", str(reply)))
        self.assertEqual(header.work, astra_work.phase_work(self.run_a, "1"))

    def test_rebind_to_an_existing_second_run_keeps_thread_and_changes_next_header(self) -> None:
        design = self.open_designer()
        first_reply = self.resume(design, "designer")
        previous = first_reply.read_bytes()
        rebound = design_roles.rebind(design.placement.design_id, expected_generation=1, reason="next phase",
                               binding=self.binding(self.run_b))
        next_reply = self.resume(rebound, "designer")
        self.assertTrue(self.run_a.is_dir())
        self.assertEqual(first_reply.read_bytes(), previous)
        self.assertNotEqual(next_reply, first_reply)
        self.assertEqual(self.launches, [("designer", "9309c903-5894-501a-8aa7-f33ea912e074")])
        self.assertEqual([entry[1] for entry in self.dispatches], ["9309c903-5894-501a-8aa7-f33ea912e074", "9309c903-5894-501a-8aa7-f33ea912e074"])
        header = astra_work.read_header(sorted(design_roles.design_directory(design.placement.design_id).glob("prompts/*.md"))[-1])
        self.assertIsInstance(header, astra_work.AstraWork)
        if isinstance(header, astra_work.AstraWork):
            self.assertEqual(header.work, astra_work.phase_work(self.run_b, "1"))
        self.assertEqual(len(self.releases), 1)

    def test_open_with_a_phase_and_no_checkout_saves_the_design_repository(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = design_roles.main(["open", "--repository", "fixture-repo", "--document", "design.md",
                "--role", "designer", "--area", "design",
                "--run-directory", str(self.run_a), "--phase", "1", "--output-path", "design.md"])
        self.assertEqual(code, 0, output.getvalue())
        design = design_roles.read_design(output.getvalue().strip().split("/")[0])
        self.assertEqual(design.binding, self.binding(self.run_a))

    def test_unbind_repairs_an_empty_repository_and_a_retry_writes_nothing(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        command = ["rebind", design.placement.design_id, "--expected-generation", "1",
                   "--unbind", "--reason", "repair historical binding"]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(design_roles.main(command), 0)
        repaired = design_roles.read_design(design.placement.design_id)
        self.assertIsInstance(repaired.binding, design_roles.DesignUnbound)
        self.assertEqual(repaired.placement.generation, 2)
        self.assertEqual(repaired.roles, design.roles)
        before = path.read_bytes(), path.stat().st_mtime_ns
        releases = list(self.releases)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(design_roles.main(command), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.releases, releases)

    def test_rebind_to_a_checkout_repairs_an_empty_binding_repository(self) -> None:
        design = self.open_designer()
        _ = damage_binding_repository(design)
        with redirect_stdout(io.StringIO()):
            code = design_roles.main(["rebind", design.placement.design_id, "--expected-generation", "1",
                "--checkout", str(self.repository), "--run-directory", str(self.run_b), "--phase", "1",
                "--output-path", "design.md", "--reason", "repair phase binding"])
        self.assertEqual(code, 0)
        repaired = design_roles.read_design(design.placement.design_id)
        self.assertEqual(repaired.binding, self.binding(self.run_b))
        self.assertEqual(repaired.placement.generation, 2)
        self.assertEqual(repaired.roles, design.roles)

    def test_binding_repair_still_refuses_a_stale_generation(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        error = io.StringIO()
        with redirect_stderr(error):
            code = design_roles.main(["rebind", design.placement.design_id, "--expected-generation", "0",
                                      "--unbind", "--reason", "stale repair"])
        self.assertEqual(code, 2)
        self.assertIn("stale binding generation", error.getvalue())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.releases, [])

    def test_checkout_repair_without_output_paths_keeps_the_existing_paths(self) -> None:
        design = self.open_designer()
        _ = damage_binding_repository(design)
        with redirect_stdout(io.StringIO()):
            code = design_roles.main(["rebind", design.placement.design_id, "--expected-generation", "1",
                "--checkout", str(self.repository), "--run-directory", str(self.run_b), "--phase", "1",
                "--reason", "repair without replacing output paths"])
        self.assertEqual(code, 0)
        repaired = design_roles.read_design(design.placement.design_id)
        self.assertEqual(repaired.binding, self.binding(self.run_b))
        self.assertEqual(repaired.placement.generation, 2)
        self.assertEqual(repaired.roles, design.roles)

    def test_invalid_rebind_target_is_refused_before_any_lifecycle_effect(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes(), path.stat().st_mtime_ns
        invalid_bindings = (
            design_roles.DesignBoundToPhase(str(self.run_b), "1", str(self.repository), ()),
            design_roles.DesignBoundToPhase("", "1", str(self.repository), ("design.md",)),
            design_roles.DesignBoundToPhase(str(self.run_b), "", str(self.repository), ("design.md",)),
        )
        for binding in invalid_bindings:
            with self.subTest(binding=binding):
                operations: list[str] = []

                def authority(operation: str, payload: dict[str, object]) -> dict[str, object]:
                    operations.append(operation)
                    return self.authority(operation, payload)

                with patch.object(design_roles, "authority_request", side_effect=authority), \
                        self.assertRaises(design_roles.RoleRefused):
                    _ = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                           reason="invalid target", binding=binding)
                self.assertNotIn("advance", operations)
                self.assertEqual(self.releases, [])
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
                self.assertFalse((path.parent / "transition.json").exists())

    def assert_interrupted_repair_finishes_publication(self, stop_at: str) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        directory = path.parent
        mesh = design_roles.mesh_directory(design.placement.design_id)
        command = ["rebind", design.placement.design_id, "--expected-generation", "1",
                   "--unbind", "--reason", "finish interrupted repair"]
        atomic = design_roles._atomic  # pyright: ignore[reportPrivateUsage]

        def interrupt(target: Path, value: object) -> None:
            if target.name == stop_at and target.name != "design.json":
                raise OSError("publication interrupted")
            atomic(target, value)
            if target.name == stop_at:
                raise OSError("publication interrupted")

        with patch.object(design_roles, "_atomic", side_effect=interrupt), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(design_roles.main(command), 2)
        committed = design_roles.read_design(design.placement.design_id)
        self.assertEqual(committed.placement.generation, 2)
        self.assertIsInstance(committed.binding, design_roles.DesignUnbound)
        if stop_at == "design_context.json":
            # The earlier writer removed this marker before publishing the context.
            (directory / "transition.json").unlink(missing_ok=True)
        # Force recovery to prove it republishes the census, rather than reading an old hold.
        (self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json").unlink(missing_ok=True)
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = design_roles.main(command)
        self.assertEqual(code, 0, error.getvalue())
        self.assertEqual(output.getvalue().splitlines(),
                         [f"{design.placement.design_id} rebound at generation 2"])
        repaired = design_roles.read_design(design.placement.design_id)
        self.assertEqual(repaired, committed)
        self.assertEqual(self.releases, [design.placement.design_id])
        context = cast(dict[str, object], json.loads((mesh / "design_context.json").read_text()))
        self.assertEqual(context["generation"], 2)
        self.assertEqual(context["binding"], {"kind": "DesignUnbound"})
        self.assertEqual([record["thread_id"] for record in astra_roles.live_roles()], ["9309c903-5894-501a-8aa7-f33ea912e074"])
        self.assertFalse((directory / "transition.json").exists())
        unchanged = {item: (item.read_bytes(), item.stat().st_mtime_ns)
                     for item in (path, mesh / "design_context.json", self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json")}
        releases = list(self.releases)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(design_roles.main(command), 0)
        self.assertEqual({item: (item.read_bytes(), item.stat().st_mtime_ns) for item in unchanged}, unchanged)
        self.assertEqual(self.releases, releases)

    def test_repair_retry_finishes_publication_after_the_design_save(self) -> None:
        self.assert_interrupted_repair_finishes_publication("design.json")

    def test_repair_retry_finishes_a_failed_launch_context_publication(self) -> None:
        self.assert_interrupted_repair_finishes_publication("design_context.json")


    def test_markerless_repair_retry_finishes_census_before_reporting_success(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        command = ['rebind', design.placement.design_id, '--expected-generation', '1',
                   '--unbind', '--reason', 'finish census publication']
        def interrupt(design: design_roles.HeldDesign, role: Role, record: staff.StaffSnapshot) -> None:
            del design, role, record
            raise OSError('census publication interrupted')

        with patch.object(design_roles, '_project_hold', side_effect=interrupt), \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(design_roles.main(command), 2)
        (path.parent / 'transition.json').unlink()
        hold = self.root / 'holds/9309c903-5894-501a-8aa7-f33ea912e074.json'
        hold.unlink()
        output = io.StringIO()
        with patch.object(design_roles, '_project_hold', side_effect=interrupt), \
                redirect_stderr(io.StringIO()), redirect_stdout(output):
            self.assertEqual(design_roles.main(command), 2)
        self.assertEqual(output.getvalue(), '')
        with redirect_stdout(output):
            self.assertEqual(design_roles.main(command), 0)
        self.assertEqual(output.getvalue().splitlines(), [f'{design.placement.design_id} rebound at generation 2'])
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual([role['thread_id'] for role in astra_roles.live_roles()],
                         ['9309c903-5894-501a-8aa7-f33ea912e074'])
        self.assertEqual(self.releases, [design.placement.design_id])

    def test_binding_repair_does_not_accept_other_record_damage(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        original = cast(dict[str, object], json.loads(path.read_text()))
        for defect in ("missing repository", "null repository", "invalid schema", "invalid role"):
            with self.subTest(defect=defect):
                fields = cast(dict[str, object], json.loads(json.dumps(original)))
                binding = cast(dict[str, object], fields["binding"])
                if defect == "missing repository":
                    del binding["repository"]
                elif defect == "null repository":
                    binding["repository"] = None
                elif defect == "invalid schema":
                    fields["schema"] = 999
                else:
                    cast(list[dict[str, object]], fields["roles"])[0]["role"] = "invalid"
                _ = path.write_text(json.dumps(fields))
                before = path.read_bytes()
                with redirect_stderr(io.StringIO()):
                    code = design_roles.main(["rebind", design.placement.design_id, "--expected-generation", "1",
                                              "--unbind", "--reason", "must not mask other damage"])
                self.assertEqual(code, 2)
                self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.releases, [])

    def test_other_record_verbs_refuse_the_damaged_path_and_binding_field(self) -> None:
        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        instance = design.placement.design_id + "/designer"
        prompt = self.root / "repair-prompt.md"
        _ = prompt.write_text("Keep the design identity.\n")
        common = ["--repository", "fixture-repo", "--document", "design.md", "--role", "designer",
                  "--area", "design"]
        commands = [
            ["open", *common],
            ["resume", instance, "--prompt-file", str(prompt)], ["end", instance], ["recover", instance],
            ["unbind-run", str(self.run_a)],
            ["rebind", design.placement.design_id, "--expected-generation", "1", "--reason", "no repair target"],
        ]
        for command in commands:
            with self.subTest(command=command[0]):
                error = io.StringIO()
                with redirect_stderr(error), redirect_stdout(io.StringIO()):
                    code = design_roles.main(command)
                self.assertEqual(code, 2, error.getvalue())
                expected = (f"design_roles: {path}: missing or invalid repository; live held role needs recovery"
                            if command[0] == "unbind-run" else
                            f"design_roles: {path}: missing or invalid binding.repository")
                self.assertEqual(error.getvalue().splitlines(), [expected])
                self.assertEqual(path.read_bytes(), before)

    def test_other_unit_scans_skip_a_damaged_record_with_one_named_diagnostic(self) -> None:

        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertIsNone(design_roles.held_document(self.repository / 'other-design.md'))
        self.assertEqual(errors.getvalue().splitlines(), [f'warning: {path}: missing or invalid repository'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.releases, [])


    def test_holding_unit_scans_keep_the_damaged_record_as_a_named_refusal(self) -> None:

        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        with self.assertRaises(design_roles.RoleRefused) as error:
            _ = design_roles.held_document(Path(design.document))
        self.assertEqual(str(error.exception), f'{path}: missing or invalid repository; live held role needs recovery')
        self.assertEqual(path.read_bytes(), before)


    def test_pending_messages_clear_only_after_successful_turn_acceptance(self) -> None:
        design = self.open_designer()
        message = design_roles.queue_message(design.placement.design_id, "designer", "holder", "Keep evidence.")
        _ = design_roles.queue_message(design.placement.design_id, "designer", "holder", "Keep evidence.", message_id=message.id)
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), (message,))
        self.fail_dispatch = True
        with self.assertRaises(design_roles.RoleRefused):
            _ = self.resume(design, "designer")
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), (message,))
        self.assertIn(message.id, self.dispatches[-1][2])
        lifecycle = self.instance(design_roles.read_design(design.placement.design_id), "designer").lifecycle
        self.assertIsInstance(lifecycle, design_roles.RoleNeedsRecovery)

    def test_successful_assignment_consumes_its_pending_ids_without_waking_on_send(self) -> None:
        design = self.open_designer()
        message = design_roles.queue_message(design.placement.design_id, "designer", "peer", "Cite the file.")
        self.assertEqual(self.dispatches, [])
        _ = self.resume(design, "designer")
        self.assertIn(message.id, self.dispatches[0][2])
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), ())

    def test_message_queued_during_a_turn_is_kept_for_the_next_assignment(self) -> None:
        design = self.open_designer()
        first = design_roles.queue_message(design.placement.design_id, "designer", "peer", "First evidence.")
        during_turn: list[design_roles.PendingMessage] = []

        def dispatch_with_message(current: design_roles.HeldDesign, role: Role, prompt: Path,
                                  reply: Path, model: str, effort: str) -> None:
            during_turn.append(design_roles.queue_message(design.placement.design_id, role, "peer", "Later evidence."))
            self.dispatch(current, role, prompt, reply, model, effort)

        with patch.object(design_roles, "dispatch_turn", side_effect=dispatch_with_message):
            _ = self.resume(design, "designer")
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), tuple(during_turn))
        self.assertIn(first.id, self.dispatches[-1][2])
        self.assertNotIn(during_turn[0].id, self.dispatches[-1][2])
        _ = self.resume(design, "designer")
        self.assertIn(during_turn[0].id, self.dispatches[-1][2])
        self.assertEqual(design_roles.pending_messages(design.placement.design_id, "designer"), ())

    def test_holder_transfer_fences_the_old_session_and_keeps_the_thread(self) -> None:

        design = self.open_designer()
        changed = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                      reason='phase change', binding=self.binding(self.run_b))
        with self.assertRaises(design_roles.RoleRefused):
            _ = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                    reason='stale generation', binding=self.binding(self.run_a))
        with patch.dict(os.environ, {'CODEX_THREAD_ID': fixture_runtime_id('different-caller')}):
            _ = self.resume(changed, 'designer')
        self.assertEqual(changed.placement.generation, 2)
        self.assertEqual(changed.roles, design.roles)
        self.assertEqual(self.launches, [('designer', '9309c903-5894-501a-8aa7-f33ea912e074')])
        self.assertEqual([entry[1] for entry in self.dispatches], ['9309c903-5894-501a-8aa7-f33ea912e074'])


    def test_holder_restart_repairs_a_missing_hold_without_replacing_the_thread(self) -> None:
        design = self.open_designer()
        (self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json").unlink()
        self.holder_session = '5dea413e-cf4a-5eba-b1b4-0b19c7e0d86e'
        parent = design_roles.worker_parent(design, "designer", staff.snapshot())
        _ = staff.update_current_session(parent.current_session, staff.native_id(self.holder_session))
        os.environ["CODEX_THREAD_ID"] = self.holder_session
        _ = self.resume(design, "designer")
        records = astra_roles.live_roles()
        self.assertEqual([(record["thread_id"], record["manager"]) for record in records],
                         [("9309c903-5894-501a-8aa7-f33ea912e074", '5dea413e-cf4a-5eba-b1b4-0b19c7e0d86e')])
        self.assertEqual(len(self.launches), 1)

    def test_role_becomes_idle_after_its_process_exits_without_a_status_write(self) -> None:
        design = self.open_designer()
        instance = self.instance(design, "designer")
        roster = design_roles.mesh_directory(design.placement.design_id) / "mesh_roster.json"
        with subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline(), "ready\n")
            data = cast(dict[str, dict[str, object]], json.loads(roster.read_text()))
            data[instance.name].update(status="running", launcher_pid=child.pid)
            _ = roster.write_text(json.dumps(data))
            before = roster.read_bytes()
            try:
                live = astra_roles.live_roles()
                self.assertEqual([role["state"] for role in live], ["busy"])
            finally:
                _ = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0)
        live = astra_roles.live_roles()
        self.assertEqual([role["state"] for role in live], ["idle"])
        self.assertEqual(roster.read_bytes(), before)

    def test_durable_hold_survives_run_removal_and_keeps_one_census_edge(self) -> None:
        design = self.open_designer()
        shutil.rmtree(self.run_a)
        records = astra_roles.live_roles()
        self.assertEqual([(record["thread_id"], record["state"]) for record in records],
                         [("9309c903-5894-501a-8aa7-f33ea912e074", "idle")])
        self.assertIsInstance(records[0]["lifetime"], astra_roles.HeldDesignRole)
        record = staff.snapshot()
        parent = design_roles.worker_parent(design, "designer", record)
        workers = record.find(kind="worker", parent=parent.id)
        self.assertEqual([(worker.current_session, worker.parent) for worker in workers],
                         [("9309c903-5894-501a-8aa7-f33ea912e074", staff.ReportsTo(parent.id))])
        self.assertEqual([(role["manager"], role["thread_id"], role["role"], role["codex_home"], role["state"])
                          for role in records],
                         [('17759e5d-1377-577b-b283-19888bbfc1f5', "9309c903-5894-501a-8aa7-f33ea912e074",
                           "designer", str(self.root / "codex-home"), "idle")])
        self.assertEqual(design_roles.read_design(design.placement.design_id).placement.design_id, design.placement.design_id)

    def test_server_refresh_uses_current_run_holder_and_generation_on_the_same_account(self) -> None:
        design = self.open_designer()
        mesh = design_roles.mesh_directory(design.placement.design_id)
        old_context = cast(dict[str, object], json.loads((mesh / "design_context.json").read_text()))
        server = mesh / "mesh_server.json"
        home = self.root / "codex-home"
        _ = server.write_text(json.dumps({"port": 32100, "pid": os.getpid(), "codex_home": str(home),
                                         "auto_compact_tokens": codex_mesh.AUTO_COMPACT_TOKENS,
                                         "design_context": old_context}))
        rebound = design_roles.rebind(design.placement.design_id, expected_generation=1, reason="run change",
                               binding=self.binding(self.run_b))
        operations: list[str] = []
        environments: list[dict[str, str]] = []

        def release_server(folder: str, *, membership: worker_lifecycle.MembershipLease,
                           owned_workers: tuple[worker_lifecycle.WorkerIdentity, ...]) -> None:
            self.assertEqual(folder, str(mesh))
            self.assertTrue(membership.covers(mesh))
            self.assertEqual(owned_workers, ())
            operations.append("release")
            server.unlink()

        def start_server(folder: str, selected_home: Path) -> int:
            operations.append("start")
            environments.append(codex_mesh._server_environment(folder, selected_home))  # pyright: ignore[reportPrivateUsage]
            return 32101

        with patch.object(codex_mesh, "_release_held_server", side_effect=release_server), \
                patch.object(codex_mesh, "_start_server", side_effect=start_server), \
                patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.run_a),
                                        "PLAN_DELEGATE_PHASE": "stale-phase"}):
            self.assertEqual(codex_mesh.ensure_server(str(mesh), home), (32101, True))
        self.assertEqual(operations, ["release", "start"])
        self.assertEqual(environments[0]["UNIT_RUN_DIR"], str(self.run_b))
        self.assertEqual(environments[0]["DESIGN_BINDING_GENERATION"], str(rebound.placement.generation))
        self.assertNotIn("PLAN_DELEGATE_PHASE", environments[0])
        self.holder_session = '69966209-b6a5-53f3-a670-35098a9425c9'
        parent = design_roles.worker_parent(design, "designer", staff.snapshot())
        _ = staff.update_current_session(parent.current_session, staff.native_id(self.holder_session))
        context = codex_mesh._server_environment(str(mesh), home)  # pyright: ignore[reportPrivateUsage]
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", context)
        self.assertEqual(design_roles.worker_parent(design, "designer", staff.snapshot()).current_session,
                         self.holder_session)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": '69966209-b6a5-53f3-a670-35098a9425c9'}):
            unbound = design_roles.rebind(design.placement.design_id, expected_generation=2, reason="run end",
                                   binding=design_roles.DesignUnbound())
        with patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.run_a), "PLAN_DELEGATE_BOARD_DIR": str(self.run_a),
                                     "UNIT_PHASE": "old"}):
            context = codex_mesh._server_environment(str(mesh), home)  # pyright: ignore[reportPrivateUsage]
        self.assertFalse(any(key.startswith(("UNIT_", "PLAN_DELEGATE_")) for key in context))
        self.assertEqual(context["DESIGN_BINDING_GENERATION"], str(unbound.placement.generation))

    def test_stale_rebind_and_busy_rebind_preserve_the_current_binding(self) -> None:
        design = self.open_designer()
        with self.assertRaisesRegex(design_roles.RoleRefused, "generation"):
            _ = design_roles.rebind(design.placement.design_id, expected_generation=2, reason="stale",
                             binding=self.binding(self.run_b))
        roster = design_roles.mesh_directory(design.placement.design_id) / "mesh_roster.json"
        entries = cast(dict[str, dict[str, object]], json.loads(roster.read_text()))
        entries[self.instance(design, "designer").name]["status"] = "running"
        _ = roster.write_text(json.dumps(entries))
        with self.assertRaisesRegex(design_roles.RoleRefused, "busy"):
            _ = design_roles.rebind(design.placement.design_id, expected_generation=1, reason="busy",
                             binding=self.binding(self.run_b))
        self.assertEqual(design_roles.read_design(design.placement.design_id).binding, design.binding)
        self.assertEqual(self.releases, [])

    def list_output(self, *arguments: str) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(design_roles.main(["list", *arguments]), 0)
        return output.getvalue()

    def test_list_prints_each_role_with_its_holder_session_thread_and_document(self) -> None:
        design = self.open_designer()
        lines = self.list_output().splitlines()
        self.assertEqual(len(lines), 2, lines)
        for role in ("designer", "reviewer"):
            instance_id = design.placement.design_id + "/" + role
            line = next(line for line in lines if instance_id in line)
            self.assertNotIn("holder", line)
            self.assertIn(design.document, line)
            self.assertNotRegex(line, r"[{}]")
            if role == "designer":
                self.assertIn("RoleOpen", line)
                self.assertIn("9309c903-5894-501a-8aa7-f33ea912e074", line)
            else:
                self.assertIn("RoleNotOpened", line)
                self.assertNotIn("thread", line)

    def test_list_names_showrunner_and_unavailable_holder_reason(self) -> None:

        design = self.open_designer()
        path = design_roles.design_directory(design.placement.design_id) / 'design.json'
        fields = cast(dict[str, object], json.loads(path.read_text()))
        fields['holder'] = {'kind': 'invalid legacy holder'}
        _ = path.write_text(json.dumps(fields))
        before = path.read_bytes(), path.stat().st_mtime_ns
        lines = self.list_output().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn(design.placement.design_id, line)
            self.assertIn(design.document, line)
            self.assertNotIn('invalid legacy holder', line)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)


    def test_list_prints_empty_inventory_and_record_problems(self) -> None:
        self.assertEqual(self.list_output(), "no held roles\n")
        bad = self.root / "roles/00000000-0000-4000-8000-000000000001/design.json"
        bad.parent.mkdir(parents=True)
        _ = bad.write_text('{"schema": 999}')
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(self.list_output(), "no held roles\n")
        lines = error.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"problem: {bad}: "), lines)
        self.assertGreater(len(lines[0]), len(f"problem: {bad}: "))
        with redirect_stderr(io.StringIO()):
            design = self.open_designer()
        error = io.StringIO()
        with redirect_stderr(error):
            lines = self.list_output().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith(design.placement.design_id + '/') for line in lines), lines)
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        self.assertIn(f"problem: {bad}: ", error.getvalue())

    def test_list_json_preserves_full_inventory_and_problems(self) -> None:
        _ = self.open_designer()
        bad = self.root / "roles/00000000-0000-4000-8000-000000000001/design.json"
        bad.parent.mkdir()
        _ = bad.write_text('{"schema": 999}')
        report = cast(dict[str, object], json.loads(self.list_output("--json")))
        inventory = cast(object, json.loads(json.dumps(design_roles.list_roles())))
        self.assertEqual(report, inventory)
        entries = cast(list[dict[str, object]], report["roles"])
        self.assertEqual(len(entries), 2)
        self.assertNotIn("holder_reach", entries[0])
        self.assertIn(str(bad), str(report["problems"]))

    def test_inventory_lists_bad_records_without_network_and_keeps_valid_roles(self) -> None:
        design = self.open_designer()
        bad = self.root / "roles/00000000-0000-4000-8000-000000000001/design.json"
        bad.parent.mkdir()
        _ = bad.write_text('{"schema": 999}')
        with patch.object(design_roles, "authority_request", side_effect=AssertionError("inventory called authority")), \
                patch.object(subprocess, "run", side_effect=AssertionError("inventory ran a command")):
            inventory = design_roles.list_roles()
        entries = cast(list[dict[str, object]], inventory["roles"])
        self.assertIn(design.placement.design_id, [entry["design_id"] for entry in entries])
        self.assertIn(str(bad), str(inventory["problems"]))

    def test_inventory_reports_paused_and_failed_threads_with_the_holder_route(self) -> None:
        design = self.open_designer()
        mesh = design_roles.mesh_directory(design.placement.design_id)
        name = self.instance(design, "designer").name
        marker = mesh / (name + ".account-move.json")
        _ = marker.write_text("{}")
        paused = cast(list[dict[str, object]], design_roles.list_roles()["roles"])
        designer = next(row for row in paused if row["role"] == "designer")
        self.assertEqual(designer["lifecycle"], {"kind": "RolePausedForMove", "staff_id": "9309c903-5894-501a-8aa7-f33ea912e074"})
        self.assertNotIn("holder_reach", designer)
        marker.unlink()
        roster = cast(dict[str, dict[str, object]], json.loads((mesh / "mesh_roster.json").read_text()))
        roster[name]["status"] = "failed"
        _ = (mesh / "mesh_roster.json").write_text(json.dumps(roster))
        failed = cast(list[dict[str, object]], design_roles.list_roles()["roles"])
        designer = next(row for row in failed if row["role"] == "designer")
        self.assertEqual(cast(dict[str, object], designer["lifecycle"])["kind"], "RoleNeedsRecovery")
        self.assertEqual(cast(dict[str, object], designer["lifecycle"])["staff_id"], "9309c903-5894-501a-8aa7-f33ea912e074")
        self.assertEqual(self.dispatches, [])

    def test_patch_checks_use_the_current_run_and_refuse_stale_or_mixed_paths(self) -> None:
        design = self.open_designer()
        rebound = design_roles.rebind(design.placement.design_id, expected_generation=1, reason="next run",
                               binding=self.binding(self.run_b))
        self.assertTrue(self.run_a.is_dir())
        accepted = design_roles.check_patch(design.placement.design_id, 2, self.repository, ["./design.md"])
        self.assertEqual(accepted, rebound.binding)
        for generation, paths in ((1, ["design.md"]), (2, ["design.md", "unowned.md"]),
                                  (2, ["design.md", "renamed.md"])):
            with self.subTest(generation=generation, paths=paths), self.assertRaises(design_roles.RoleRefused):
                _ = design_roles.check_patch(design.placement.design_id, generation, self.repository, paths)
        _ = (self.run_b / "phases/1/delegated_phase_reservation_state.json").write_bytes(b"\xff")
        with self.assertRaises((ValueError, OSError)):
            _ = design_roles.check_patch(design.placement.design_id, 2, self.repository, ["design.md"])

    def test_a_binding_to_an_ad_hoc_phase_open_beside_another_accepts_its_patch(self) -> None:
        phase = self.run_a / "phases/adhoc"
        phase.mkdir()
        _ = (phase / "delegated_phase_reservation_state.json").write_text(json.dumps({
            "kind": "enrolled_awaiting_first_touch", "phase": "ad hoc"}))
        _ = (phase / "files").write_text("design.md\n")
        state = self.run_a / "progress_history_state.json"
        recorded = cast(dict[str, object], json.loads(state.read_text()))
        ad_hoc = {"id": "ad hoc", "instance_id": "instance-ad-hoc", "status": "active"}
        recorded["open_phases"] = [*cast(list[object], recorded["open_phases"]), ad_hoc]
        _ = state.write_text(json.dumps(recorded))
        binding = design_roles.DesignBoundToPhase(str(self.run_a), "ad hoc", str(self.repository), ("design.md",))
        design = design_roles.open_role("fixture-repo", "design.md", "designer", "design", binding=binding, request_id="open-ad-hoc")
        self.assertEqual(design_roles.check_patch(design.placement.design_id, 1, self.repository, ["design.md"]), binding)

    def test_patch_cannot_follow_a_symlink_outside_the_reserved_repository(self) -> None:
        design = self.open_designer()
        outside = self.root / "outside.md"
        _ = outside.write_text("Private evidence")
        (self.repository / "design.md").unlink()
        (self.repository / "design.md").symlink_to(outside)
        with self.assertRaises(design_roles.RoleRefused):
            _ = design_roles.check_patch(design.placement.design_id, 1, self.repository, ["design.md"])
        self.assertEqual(outside.read_text(), "Private evidence")

    def test_unbind_keeps_evidence_and_threads_but_refuses_the_next_writer_turn(self) -> None:
        design = self.open_designer()
        reply = self.resume(design, "designer")
        saved = {path: path.read_bytes() for path in design_roles.design_directory(design.placement.design_id).glob("prompts/*.md")}
        design_roles.unbind_run(str(self.run_a))
        unbound = design_roles.read_design(design.placement.design_id)
        self.assertIsInstance(unbound.binding, design_roles.DesignUnbound)
        self.assertEqual(self.instance(unbound, "designer").lifecycle, design_roles.RoleOpen(staff.StaffId("9309c903-5894-501a-8aa7-f33ea912e074")))
        with self.assertRaises(design_roles.RoleRefused):
            _ = self.resume(unbound, "designer")
        for path, contents in saved.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertTrue(reply.is_file())
        self.assertEqual(len(self.launches), 1)

    def test_unbind_run_rerun_with_a_trailing_slash_finishes_the_saved_transition(self) -> None:
        design = self.open_designer()
        identifier = design.placement.design_id
        arguments = ["unbind-run", str(self.run_a) + "/"]
        with stop_once(design_roles, "_save"), self.assertRaises(InjectedStop):
            _ = design_roles.main(arguments)
        saved = design_roles.read_design(identifier)
        self.assertIsInstance(saved.binding, design_roles.DesignUnbound)
        self.assertEqual(design_roles.main(arguments), 0)
        self.assertEqual(design_roles.read_design(identifier), saved)
        self.assertFalse((design_roles.design_directory(identifier) / "transition.json").exists())
        context = cast(dict[str, object], json.loads(
            (design_roles.mesh_directory(identifier) / "design_context.json").read_text()))
        self.assertEqual(context["generation"], saved.placement.generation)
        self.assertEqual(context["binding"], {"kind": "DesignUnbound"})
        self.assertEqual(saved.roles, design.roles)
        self.assertEqual(len(self.launches), 1)

    def test_end_is_terminal_and_keeps_saved_prompts(self) -> None:
        design = self.open_designer()
        folder = design_roles.design_directory(design.placement.design_id)
        saved = {path: path.read_bytes() for path in folder.glob("prompts/*.md")}
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(codex_mesh, "held_thread_end_confirmed", return_value=True):
            ended = design_roles.end(design.placement.design_id + "/designer")
            retried = design_roles.end(design.placement.design_id + "/designer")
        self.assertIsInstance(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded)
        self.assertIsInstance(self.instance(retried, "designer").lifecycle, design_roles.RoleEnded)
        with self.assertRaisesRegex(design_roles.RoleRefused, "role ended; this design identity cannot be reopened"):
            _ = self.open_designer()
        self.assertEqual(len(self.launches), 1)
        self.assertFalse((self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json").exists())
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)

    def test_unit_rename_retry_keeps_both_original_threads_and_updates_live_paths(self) -> None:

        design = self.open_designer()
        design = design_roles.open_role('fixture-repo', 'design.md', 'reviewer', 'design', binding=self.binding(self.run_a))
        initial_prompts = {path: path.read_bytes() for path in design_roles.design_directory(design.placement.design_id).glob('prompts/*.md')}
        destination = self.root / 'renamed-checkout'
        _ = self.repository.rename(destination)
        current = design_roles.rebind(design.placement.design_id, expected_generation=1, reason='checkout move',
                                      repository=str(destination), document=str(destination / 'design.md'),
                                      binding=replace(self.binding(self.run_a), repository=str(destination)))
        self.assertEqual(current.repository, str(destination))
        self.assertEqual(current.document, str(destination / 'design.md'))
        self.assertEqual(current.placement.generation, 2)
        self.assertEqual(current.roles, design.roles)
        self.assertEqual({path: path.read_bytes() for path in initial_prompts}, initial_prompts)
        for role in ('designer', 'reviewer'):
            _ = self.resume(current, role)
        self.assertEqual([(role, thread) for role, thread, _text in self.dispatches],
                         [('designer', '9309c903-5894-501a-8aa7-f33ea912e074'), ('reviewer', '364898ad-f177-5514-80f3-b82001fa95d9')])
        self.assertEqual(len(self.launches), 2)



    def test_a_unit_transfer_moves_the_holder_to_the_receiving_production(self) -> None:

        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / 'design.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        receiver = fixture_member('showrunner', name='receiving-production')
        _ = staff.move(parent.id, staff.ReportsTo(receiver.id))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = self.resume(design, 'designer')
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual(self.dispatches[-1][1], '9309c903-5894-501a-8aa7-f33ea912e074')
        self.assertEqual(len(self.launches), 1)



    def test_unit_rename_skips_another_units_damaged_record(self) -> None:

        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / 'design.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        _ = staff.rename(parent.id, 'renamed-parent')
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = self.resume(design, 'designer')
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual(self.dispatches[-1][1], '9309c903-5894-501a-8aa7-f33ea912e074')
        self.assertEqual(len(self.launches), 1)


    def test_unit_transfer_skips_another_units_damaged_record(self) -> None:

        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / 'design.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        receiver = fixture_member('showrunner', name='receiving-production')
        _ = staff.move(parent.id, staff.ReportsTo(receiver.id))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = self.resume(design, 'designer')
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual(self.dispatches[-1][1], '9309c903-5894-501a-8aa7-f33ea912e074')
        self.assertEqual(len(self.launches), 1)


    def test_holder_move_refuses_its_own_damaged_record_by_path_and_field(self) -> None:

        design = self.open_designer()
        path = damage_binding_repository(design)
        before = path.read_bytes()
        with self.assertRaises(worker_lifecycle.WorkerStateUnreadable) as error:
            _ = design_roles.rebind(design.placement.design_id, expected_generation=1, reason='phase change')
        self.assertIn(str(path), str(error.exception))
        self.assertIn('repository', str(error.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.releases, [])


    def test_a_production_rename_moves_the_showrunner_holder(self) -> None:

        design = self.open_designer()
        directory = design_roles.design_directory(design.placement.design_id)
        path = directory / 'design.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        _ = staff.rename(parent.id, 'renamed-parent')
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        _ = self.resume(design, 'designer')
        self.assertEqual(design_roles.read_design(design.placement.design_id).roles, design.roles)
        self.assertEqual(self.dispatches[-1][1], '9309c903-5894-501a-8aa7-f33ea912e074')
        self.assertEqual(len(self.launches), 1)


    def test_uncertain_server_release_blocks_rename_before_holder_changes(self) -> None:

        design = self.open_designer()
        with patch.object(design_roles, 'release_servers', side_effect=design_roles.RoleRefused('release uncertain')):
            with self.assertRaisesRegex(design_roles.RoleRefused, 'uncertain'):
                _ = design_roles.rebind(design.placement.design_id, expected_generation=1,
                                        reason='phase change', binding=self.binding(self.run_b))
        self.assertEqual(design_roles.read_design(design.placement.design_id), design)




    def test_last_role_end_stops_the_server_and_preserves_design_evidence(self) -> None:
        design = self.open_designer()
        design = design_roles.open_role("fixture-repo", "design.md", "reviewer", "design",
                                 binding=self.binding(self.run_a))
        design_id = design.placement.design_id
        directory, mesh = design_roles.design_directory(design_id), design_roles.mesh_directory(design_id)
        _ = self.resume(design, "reviewer")
        message = design_roles.queue_message(design_id, "reviewer", "fixture", "Keep this evidence after end.")
        evidence = {path: path.read_bytes() for folder in ("prompts", "replies", "pending")
                    for path in (directory / folder).rglob("*") if path.is_file()}
        authority_before = self.authority("lookup", {"repository_id": "fixture-repo", "document": "design.md",
                                                  "host": "natedev"})
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def clean_process() -> None:
            if process.poll() is None:
                process.kill()
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
            if process.stdin is not None:
                process.stdin.close()

        self.addCleanup(clean_process)
        server = mesh / "mesh_server.json"
        _ = server.write_text(json.dumps({"pid": process.pid, "port": 32100,
                                         "codex_home": str(self.root / "codex-home")}))
        methods: list[str] = []

        def request(method: str, params: dict[str, object], **options: object) -> dict[str, object]:
            del options
            methods.append(method)
            if method == "thread/queue/list":
                return {"result": {"data": []}}
            if method == "thread/read":
                return {"result": {"thread": {"id": params["threadId"], "status": {"type": "idle"}, "turns": []}}}
            if method == "thread/unsubscribe":
                return {"result": {"status": "notLoaded"}}
            raise AssertionError("unexpected end request: " + method)

        def end_command(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            del options
            self.assertEqual(command[2], "end")
            result = codex_mesh.command_end(argparse.Namespace(session_dir=str(mesh), to=command[-1]))
            return subprocess.CompletedProcess(command, result, "", "")

        client = cast(codex_mesh.Client, MagicMock(spec=codex_mesh.Client))
        with patch.object(subprocess, "run", side_effect=end_command), \
                patch.object(codex_mesh, "Client", return_value=client), patch.object(client, "call", side_effect=request), \
                patch.object(design_roles, "release_servers", side_effect=release_design_servers), \
                patch.object(codex_mesh, "REAP_WAIT_SECS", 0.05), patch.object(codex_mesh, "REAP_POLL_SECS", 0.001):
            first = design_roles.end(design_id + "/designer")
            self.assertIsInstance(self.instance(first, "designer").lifecycle, design_roles.RoleEnded)
            self.assertIsInstance(self.instance(first, "reviewer").lifecycle, design_roles.RoleOpen)
            self.assertIsNone(process.poll())
            self.assertTrue(server.exists())
            self.assertNotIn("thread/unsubscribe", methods)
            ended = design_roles.end(design_id + "/reviewer")
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assertTrue(all(isinstance(instance.lifecycle, design_roles.RoleEnded) for instance in ended.roles))
        self.assertFalse(server.exists())
        self.assertIn("thread/unsubscribe", methods)
        roster = cast(dict[str, dict[str, object]], json.loads((mesh / "mesh_roster.json").read_text()))
        self.assertEqual({entry["staff_id"] for entry in roster.values()}, {"9309c903-5894-501a-8aa7-f33ea912e074", "364898ad-f177-5514-80f3-b82001fa95d9"})
        self.assertTrue(all(entry["status"] == "ended" for entry in roster.values()))
        self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
        self.assertEqual(design_roles.pending_messages(design_id, "reviewer"), (message,))
        self.assertEqual(self.authority("lookup", {"repository_id": "fixture-repo", "document": "design.md",
                                                 "host": "natedev"}), authority_before)

    def test_end_retry_requires_proof_that_the_original_turn_is_no_longer_live(self) -> None:
        design = self.open_designer()
        instance_id = design.placement.design_id + "/designer"
        active = True
        client = cast(codex_mesh.Client, MagicMock(spec=codex_mesh.Client))

        def request(method: str, params: object, **options: object) -> dict[str, object]:
            del params, options
            if method == "thread/turns/list":
                return {"result": {"data": [{"id": "still-running", "status": "inProgress"}] if active else []}}
            if method != "thread/read":
                raise AssertionError("unexpected cleanup proof request: " + method)
            return {"result": {"thread": {"id": "9309c903-5894-501a-8aa7-f33ea912e074", "status": {"type": "active" if active else "idle"}}}}

        mesh = design_roles.mesh_directory(design.placement.design_id)
        _ = (mesh / "mesh_server.json").write_text(json.dumps({"pid": os.getpid(), "port": 32100}))
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(design_roles.RoleRefused, "RoleNeedsRecovery"):
                _ = design_roles.end(instance_id)
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                patch.object(codex_mesh, "Client", return_value=client), patch.object(client, "call", side_effect=request):
            with self.assertRaisesRegex(design_roles.RoleRefused, "RoleNeedsRecovery"):
                _ = design_roles.end(instance_id)
            self.assertIsInstance(self.instance(design_roles.read_design(design.placement.design_id), "designer").lifecycle,
                                  design_roles.RoleNeedsRecovery)
            self.assertTrue((self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json").exists())
            active = False
            ended = design_roles.end(instance_id)
        self.assertEqual(self.instance(ended, "designer").lifecycle, design_roles.RoleEnded(staff.StaffId("9309c903-5894-501a-8aa7-f33ea912e074")))
        self.assertFalse((self.root / "holds/9309c903-5894-501a-8aa7-f33ea912e074.json").exists())

    def test_showrunner_notifier_target_requires_a_live_local_session(self) -> None:

        design = self.open_designer()
        parent = design_roles.worker_parent(design, 'designer', staff.snapshot())
        self.assertEqual(parent.current_session, self.holder_session)
        with patch.object(staff.StaffSnapshot, 'by_id', return_value=None):
            with self.assertRaisesRegex(design_roles.RoleRefused, 'parent is unavailable'):
                _ = design_roles.worker_parent(design, 'designer', staff.snapshot())




if __name__ == "__main__":
    _ = unittest.main()
