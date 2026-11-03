#!/usr/bin/env python3
"""Read production fields and Units table cells without resolving staff membership."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

for directory in (Path(__file__).resolve().parent, Path(__file__).resolve().parents[2]):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
from collections.abc import Iterable
import unit_names

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone  # noqa: E402


from scripts.production.unit_table import (
    Refusal as Refusal, cell_value as cell_value, unit_rows as unit_rows,
    row_cells as row_cells, unit_headings as unit_headings, unit_table as unit_table,
)


WRAPPED = "> **Status: PRODUCTION — wrapped.**"


class Production(NamedTuple):
    doc: Path
    slug: str
    merge_branch: str
    checkout: Path
    log: Path
    zone: ZoneInfo


def plan_arguments(plan: str) -> list[str]:
    """Translate a Units Plan cell into add_unit's launch mode."""
    return ["--" + plan] if plan in ("standby", "permanent") else ["--plan", plan]


def plan_cell_is_retired(plan_cell: str) -> bool:
    """Return whether a Plan cell begins with the retired marker."""
    return re.match(r"^\s*\(?retired\b", plan_cell) is not None


def worktree_is_linked(worktree: str) -> bool:
    """Return whether `worktree` is a linked worktree on disk.

    A main checkout, where `.git` is a directory, is shared and is no unit's own worktree.
    """
    return bool(worktree) and (Path(worktree) / ".git").is_file()


def order_unit_names(lines: list[str], names: Iterable[str], *, offline: Iterable[str] = ()) -> list[str]:
    """Keep document presentation order without letting it decide membership."""
    wanted = set(names)
    by_name = {unit_names.name(name): name for name in wanted}
    unplaced = set(offline)
    ordered: list[str] = []
    try:
        for cells in unit_table(lines):
            displayed = cell_value(cells.get("Unit", ""))
            name = by_name.get(unit_names.name(displayed), "")
            if name in wanted:
                ordered.append(displayed if name in unplaced else name)
                wanted.remove(name)
    except (Refusal, unit_names.BadName):
        pass
    return ordered + sorted(wanted)


def production_field(lines: list[str], field: str) -> str:
    """Read a Production Context field without interpreting its commentary."""
    prefix = f"- **{field}:** "
    for line in lines:
        if line.startswith(prefix):
            result = cell_value(line[len(prefix):])
            if result:
                return result
    raise Refusal(f"production doc lacks {field}")


def read_production(path: Path) -> Production:
    """Read the production fields shared with merge_checkpoint.py."""
    try:
        doc = path.resolve(strict=True)
        lines = doc.read_text(encoding="utf-8").splitlines()
        merge_branch = production_field(lines, "Merge branch")
        checkout = Path(production_field(lines, "Showrunner checkout")).expanduser().resolve()
        log = Path(production_field(lines, "Log"))
        zone = user_zone()
        slug = doc.name.removesuffix("-production.md")
        if slug == doc.name:
            raise Refusal("production doc name must end in -production.md")
        if log.is_absolute():
            raise Refusal("production Log must be relative to Showrunner checkout")
        if "## Units" not in lines:
            raise Refusal("production doc lacks Units table")
        return Production(doc, slug, merge_branch, checkout, checkout / log, zone)
    except (OSError, UnicodeError, KeyError, ValueError) as error:
        raise Refusal(f"cannot read production doc: {error}") from error
