"""Durable-role records shared by lifecycle, review and messaging regressions."""
from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
from typing import cast
from uuid import uuid4

from scripts.production import staff
from scripts.production.staff_test_support import fixture_worker, unit_membership
from scripts.astra import design_roles
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.server_test_support import owned_server_record

_NOT_OPENED = design_roles.RoleNotOpened()


@contextmanager
def write_mesh_server_record(mesh: Path, codex_home: Path) -> Generator[None]:
    write_text_atomic(mesh / "mesh_server.json", json.dumps({
        "codex_home": str(codex_home), "port": 32100,
    }))
    with owned_server_record(mesh / "mesh_server.json"):
        yield


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
