"""The one naming rule for a production unit: its id, stem, branch and worktree.

`add_unit.py` names a new unit with it, and both renames name the destination with it, so a
unit created, renamed or moved with its production always gets the same names.
"""
from __future__ import annotations

import re
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class BadName(ValueError):
    pass


def name(given: str) -> str:
    """The unit's name without its `-unit` suffix; a given id is not suffixed twice."""
    if not NAME.fullmatch(given):
        raise BadName("name must contain only letters, numbers, hyphens, or underscores")
    return given[: -len("-unit")] if given.replace("_", "-").endswith("-unit") else given


def unit_id(given: str) -> str:
    return f"{name(given)}-unit"


def stem(unit: str) -> str:
    """The branch and worktree stem of a unit id: its name with underscores as hyphens."""
    return unit.removesuffix("-unit").replace("_", "-")


def branch(slug: str, unit_stem: str) -> str:
    """The slug names the branch, as it names the production; the merge branch may differ."""
    return f"{slug}-{unit_stem}"


def worktree(checkout: Path, unit_stem: str) -> Path:
    return checkout.parent / f"{checkout.name.removesuffix('-trunk')}-{unit_stem}"


def swap_stem(value: str, old_stem: str, new_stem: str) -> str | None:
    """A branch or worktree name with its trailing `-<old stem>` replaced, or None without one."""
    return value[: -len(old_stem)] + new_stem if value.endswith(f"-{old_stem}") else None
