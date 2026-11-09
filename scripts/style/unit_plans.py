"""Read the style units' standing plans: their Delegation Context and pilot scopes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.style import rules, style_config
from scripts.style import style_ledger
from scripts.production import unit_config
from scripts.style.unit_files import unit_worktrees


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


def unit_plan_path(unit: str, worktree: Path) -> Path:
    """The plan file that the unit's `.unit.toml` names."""
    match unit_config.read(worktree, cleanup=False):
        case unit_config.UnitConfig(plan=unit_config.PlanFile(path)):
            return worktree / path
        case _:
            raise style_ledger.LedgerError(f"{unit}: no plan file in {worktree / '.unit.toml'}")


def family_plan_texts(doc: Path) -> Mapping[rules.Family, str | MissingUnitPlan]:
    """Read each style unit's own standing plan copy."""
    texts: dict[rules.Family, str | MissingUnitPlan] = {}
    config = style_config.read_config(doc, unit_branch="")
    worktrees = {row.unit: row.worktree for row in unit_worktrees(doc)}
    for assignment in config.assignments:
        if assignment.unit not in worktrees:
            continue
        text: str | MissingUnitPlan = MissingUnitPlan()
        try:
            text = unit_plan_path(assignment.unit, worktrees[assignment.unit]).read_text()
        except (FileNotFoundError, style_ledger.LedgerError):
            pass
        for family in assignment.families:
            texts[family] = text
    return texts


def family_pilot_scopes(doc: Path, repo: Path) -> Mapping[rules.Family, PilotScope]:
    return {family: parse_pilot_scope(text, repo) if isinstance(text, str) else AllCrates()
            for family, text in family_plan_texts(doc).items()}
