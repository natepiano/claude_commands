"""Durable-role records shared by lifecycle, review and messaging regressions."""
from __future__ import annotations

import json
import io
import subprocess
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from dataclasses import replace
import os
from pathlib import Path
from typing import cast, final, override
from uuid import uuid4

from scripts.production import staff
from scripts.production.staff_test_support import fixture_worker, unit_membership
from scripts.astra import design_roles
from scripts.lib.atomic_file import write_text_atomic
from scripts.direct.recorder_fixture import invoke_recorder

_NOT_OPENED = design_roles.RoleNotOpened()


def designer_fixture(parent: staff.StaffId, name: str, codex_home: Path) -> staff.StaffRow:
    """Publish a private designer under its staff parent."""
    return fixture_worker(name, parent, backend=staff.CodexBackend(codex_home))


def write_empty_binding_record(repository: Path, *,
                               lifecycle: design_roles.Lifecycle = _NOT_OPENED,
                               run_directory: str = '') -> Path:
    design_id = str(uuid4())
    designer_name = 'broken-designer'
    if not isinstance(lifecycle, design_roles.RoleNotOpened):
        row = designer_fixture(unit_membership("fixture", "broken-unit").id, "broken-" + design_id[:8] + "-designer",
                               Path(os.environ.get("CODEX_HOME", str(repository.parent / "codex-home"))))
        lifecycle = replace(lifecycle, staff_id=row.id)
        designer_name = row.name
    design = design_roles.HeldDesign(
        design_roles.DesignPlacement(design_id, 'fixture-host', 1), 'fixture-repo', str(repository),
        str(repository / 'broken-design.md'), 'design', '2026-10-31T00:00:00+00:00',
        design_roles.DesignBoundToPhase(run_directory or str(repository / 'broken-run'), '1',
                                       str(repository), ('broken-design.md',)),
        (design_roles.RoleInstance('designer', designer_name, lifecycle),
         design_roles.RoleInstance('reviewer', 'broken-reviewer', design_roles.RoleNotOpened())))
    design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
    path = design_roles.design_directory(design_id) / 'design.json'
    fields = cast(dict[str, object], json.loads(path.read_text()))
    binding = cast(dict[str, object], fields['binding'])
    binding['repository'] = ''
    _ = path.write_text(json.dumps(fields))
    return path


@final
class HeldRolePaneFixture:
    """Use the real worker command with private pane and transcript fixtures."""

    def __init__(self, case: unittest.TestCase) -> None:
        from scripts.production import test_worker as worker_tests

        self.command_case = worker_tests.WorkerCommandTests("runTest")
        self.command_case.setUp()
        case.addCleanup(self.command_case.doCleanups)
        self.root = self.command_case.fixture.root
        self.command_directory = self.root
        self.world = self.command_case.fixture.world
        self.router = worker_tests.PaneCommandRouter()
        self.router.setup(case, self.root)
        self.commands: list[list[str]] = []
        self.recorder_commands: list[list[str]] = []
        self.router.recorder_commands = self.recorder_commands
        write_text_atomic(self.root / "design.md", "# Private design\n")
        _ = cast(object, case.enterContext(patch.dict(os.environ, {
            "DESIGN_ROLES_STATE": str(self.root / "designs"),
            "DESIGN_OWNERSHIP_STATE": str(self.root / "ownership"),
            "ASTRA_ROLES_STATE": str(self.root / "holds"),
            "DESIGN_ROLES_HOST": "natedev",
            "CODEX_HOME": str(self.root / "codex"),
        })))
        _ = case.enterContext(patch.object(design_roles, "_worker_command", side_effect=self.command))
        _ = case.enterContext(patch.object(design_roles, "_assign_command", side_effect=self.assign))
        _ = case.enterContext(patch.object(design_roles, "_recorder_command", side_effect=self.record))
        design_roles.register_repository("private-repo", {"natedev": str(self.root)})
        state_path = self.command_case.run_dir / "progress_history_state.json"
        state = cast(dict[str, object], json.loads(state_path.read_text()))
        state["run_id"] = "held-role-private-run"
        write_text_atomic(state_path, json.dumps(state))
        phase = self.command_case.run_dir / "phases/8"
        phase.mkdir(parents=True)
        write_text_atomic(phase / "delegated_phase_reservation_state.json",
                          json.dumps({"kind": "enrolled_awaiting_first_touch", "phase": "8"}))
        write_text_atomic(phase / "files", "design.md\n")

    @property
    def binding(self) -> design_roles.DesignBoundToPhase:
        return design_roles.DesignBoundToPhase(str(self.command_case.run_dir), "8", str(self.root), ("design.md",))

    def command(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        from scripts.production import worker

        self.commands.append(arguments.copy())
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            status = worker.main(arguments)
        return subprocess.CompletedProcess(arguments, status, output.getvalue(), errors.getvalue())

    def assign(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        from scripts.direct import assign
        from scripts.production.cli_fixture import invoke_cli

        self.commands.append(arguments.copy())
        return invoke_cli(assign, assign.main, arguments, os.environ.copy(), self.command_directory)

    def record(self, arguments: list[str], *, env: Mapping[str, str] = os.environ) -> None:
        self.recorder_commands.append(arguments.copy())
        result = invoke_recorder(arguments, env, self.root)
        if result.returncode:
            raise design_roles.RoleRefused(result.stderr.strip() or "held dispatch recording did not complete")

    def open(self, role: design_roles.Role, *, bound: bool = True) -> design_roles.HeldDesign:
        return design_roles.open_role("private-repo", "design.md", role, "design",
                                      binding=self.binding if bound else design_roles.UNBOUND)

    def resume(self, design: design_roles.HeldDesign, role: design_roles.Role) -> Path:
        submitted = design_roles.resume(design.placement.design_id + "/" + role, self.command_case.prompt)
        self.finish(design, role)
        return submitted.reply

    def finish(self, design: design_roles.HeldDesign, role: design_roles.Role) -> None:
        from scripts.herdr import turn_end

        instance = next(item for item in design.roles if item.role == role)
        if not isinstance(instance.lifecycle, (design_roles.RoleOpen, design_roles.RolePausedForMove)):
            raise AssertionError("role has no worker")
        row = staff.by_id(instance.lifecycle.staff_id)
        if row is None:
            raise AssertionError("role worker is missing")
        thread = str(row.current_session)
        with self.world.process_starts():
            _ = turn_end.handle(turn_end.WorkerEvent(thread, reply=self.command_case.agent.last_replies.get(thread, "")))


class HeldRolePaneCase(unittest.TestCase):
    """Create the shared pane fixture during unittest setup."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixtures: list[HeldRolePaneFixture] = []

    @property
    def panes(self) -> HeldRolePaneFixture:
        return self.fixtures[0]

    @override
    def setUp(self) -> None:
        self.fixtures.append(HeldRolePaneFixture(self))
