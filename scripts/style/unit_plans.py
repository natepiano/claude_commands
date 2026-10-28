"""Read the style units' standing plans: their Delegation Context and pilot scopes."""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

import rules
import style_ledger
from production_units import cell_value, unit_table
from unit_files import unit_worktrees


@dataclass(frozen=True)
class AllCrates:
    pass


@dataclass(frozen=True)
class PilotCrates:
    members: frozenset[str]


PilotScope = AllCrates | PilotCrates


def crate_in_scope(scope: PilotScope, crate: str) -> bool:
    return isinstance(scope, AllCrates) or crate in scope.members


def delegation_context(text: str) -> str:
    match = re.search(r"(?ms)^## Delegation Context\n(.*?)(?=^## |\Z)", text)
    if match is None:
        raise style_ledger.LedgerError("plan lacks Delegation Context")
    return match.group(1).strip()


def parse_pilot_scope(text: str, repo: Path) -> PilotScope:
    """Resolve the supplied plan's pilot package names to member directories."""
    lines: list[str] = [match.group(1) for match in re.finditer(
        r"(?m)^\s*(?:- )?\*\*Pilot scope:\*\*[^\S\n]*(.*)$", delegation_context(text))]
    if not lines:
        return AllCrates()
    if len(lines) > 1:
        raise style_ledger.LedgerError("pilot scope has more than one scope line")
    manifests = {path.relative_to(repo).as_posix(): path.read_text()
                 for path in repo.rglob("Cargo.toml") if path.is_file()}
    members = {member.package_name: member.directory for member in rules.workspace_members(manifests)}
    names = tuple(name.strip() for name in lines[0].split(","))
    for name in names:
        if name not in members:
            raise style_ledger.LedgerError(f"pilot scope names unknown crate {name}")
    return PilotCrates(frozenset(members[name] for name in names))


@dataclass(frozen=True)
class MissingUnitPlan:
    pass


def unit_plan_path(doc: Path, unit: str, worktree: Path) -> Path:
    for row in unit_table(doc.read_text().splitlines()):
        if cell_value(row["Unit"]) == unit:
            return worktree / Path(cell_value(row["Plan"]))
    raise style_ledger.LedgerError(f"{unit}: no Units row in {doc}")


def family_plan_texts(doc: Path) -> Mapping[rules.Family, str | MissingUnitPlan]:
    """Read each style unit's own standing plan copy."""
    texts: dict[rules.Family, str | MissingUnitPlan] = {}
    for row in unit_worktrees(doc):
        family = rules.style_family(row.unit)
        if isinstance(family, rules.NotStyleUnit):
            continue
        plan = unit_plan_path(doc, row.unit, row.worktree)
        try:
            texts[family] = plan.read_text()
        except FileNotFoundError:
            texts[family] = MissingUnitPlan()
    return texts


def family_pilot_scopes(doc: Path, repo: Path) -> Mapping[rules.Family, PilotScope]:
    return {family: parse_pilot_scope(text, repo) if isinstance(text, str) else AllCrates()
            for family, text in family_plan_texts(doc).items()}
