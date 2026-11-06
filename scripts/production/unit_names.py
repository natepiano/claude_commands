"""Derive branch and worktree names from the unit staff name."""
from __future__ import annotations

import re
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class BadName(ValueError):
    pass


def name(given: str) -> str:
    """The unit's staff name."""
    if not NAME.fullmatch(given):
        raise BadName("name must contain only letters, numbers, hyphens, or underscores")
    return given


def stem(unit: str) -> str:
    """Return the staff name with underscores as hyphens."""
    return unit.replace("_", "-")


def branch(production_name: str, unit_stem: str) -> str:
    """Prefix the branch with the production name when necessary."""
    return unit_stem if unit_stem.startswith(f"{production_name}-") else f"{production_name}-{unit_stem}"


def worktree(checkout: Path, unit_stem: str) -> Path:
    return checkout.parent / branch(checkout.name.removesuffix("-trunk"), unit_stem)


def swap_stem(value: str, old_stem: str, new_stem: str) -> str | None:
    """A branch or worktree name with its trailing `-<old stem>` replaced, or None without one."""
    return value[: -len(old_stem)] + new_stem if value.endswith(f"-{old_stem}") else None
