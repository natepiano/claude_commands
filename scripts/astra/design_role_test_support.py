"""Durable-role records shared by lifecycle, review and messaging regressions."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast
from uuid import uuid4

from scripts.astra import design_roles
from scripts.lib.atomic_file import write_text_atomic

_NOT_OPENED = design_roles.RoleNotOpened()


def write_mesh_server_record(mesh: Path, codex_home: Path) -> None:
    write_text_atomic(mesh / "mesh_server.json", json.dumps({
        "codex_home": str(codex_home), "pid": os.getpid(), "port": 32100,
    }))


def write_empty_binding_record(repository: Path, holder: design_roles.Holder, *,
                               lifecycle: design_roles.Lifecycle = _NOT_OPENED,
                               run_directory: str = '') -> Path:
    design_id = str(uuid4())
    design = design_roles.HeldDesign(
        design_roles.DesignPlacement(design_id, 'fixture-host', 1), 'fixture-repo', str(repository),
        str(repository / 'broken-design.md'), 'design', '2026-10-31T00:00:00+00:00', holder,
        design_roles.DesignBoundToPhase(run_directory or str(repository / 'broken-run'), '1',
                                       str(repository), ('broken-design.md',)),
        (design_roles.RoleInstance('designer', 'broken-designer', lifecycle),
         design_roles.RoleInstance('reviewer', 'broken-reviewer', design_roles.RoleNotOpened())))
    design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
    path = design_roles.design_directory(design_id) / 'design.json'
    fields = cast(dict[str, object], json.loads(path.read_text()))
    binding = cast(dict[str, object], fields['binding'])
    binding['repository'] = ''
    _ = path.write_text(json.dumps(fields))
    return path
