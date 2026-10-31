"""The one naming rule for a production unit: its id, short name, branch and worktree.

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
    return given[: -len("-unit")] if name_key(given).endswith("-unit") else given


def unit_id(given: str) -> str:
    return f"{name(given)}-unit"


def stem(unit: str) -> str:
    """The short name of a unit id, preserved verbatim for its branch and folder."""
    return unit.removesuffix("-unit")


def branch(unit_stem: str) -> str:
    """The globally unique short name; the production does not prefix it."""
    return unit_stem


def worktree(unit_stem: str) -> Path:
    return Path.home() / "worktrees" / unit_stem


def name_key(short_name: str) -> str:
    """Treat underscores and hyphens as one spelling without changing stored names."""
    return short_name.replace("_", "-")
