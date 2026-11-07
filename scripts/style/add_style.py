#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Create or attach one standing style production per repository."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from typing import cast

SCRIPTS = Path(__file__).resolve().parents[1]

from scripts.production import showrunners, staff
from scripts.lib.git_exclude import ensure_excluded
from scripts.production import showrunner_tree
from scripts.production.add_showrunner import NoEndingTab, herdr_tab_open
from scripts.herdr import herdr
from scripts.production.production_doc import Production, Refusal, production_field, read_production, read_new_production, optional_field, FieldPresent

from scripts.style import rules
from scripts.style import style_ledger
from scripts.lib.atomic_file import write_text_atomic

FAMILIES: tuple[rules.Family, ...] = ("names", "values", "types", "modules")
PLAN_DIRECTORY = Path("docs/style-production/units")
LAUNCHER = Path(os.environ.get("ADD_STYLE_LAUNCHER") or SCRIPTS / "production/add_showrunner.py")
SHOWRUNNERS = Path(os.environ.get("ADD_STYLE_SHOWRUNNERS") or SCRIPTS / "production/showrunners.py")


class StyleBranchPointTaken(Refusal):
    """The repository's standing production already follows another branch point."""


@dataclass(frozen=True)
class PilotUnit:
    family: rules.Family
    packages: tuple[str, ...]  # An empty selection covers every workspace package.


@dataclass(frozen=True)
class AllStyleUnits:
    """Launch all four families without crate restrictions."""


@dataclass(frozen=True)
class PilotUnits:
    units: tuple[PilotUnit, ...]


def _pilot_units(arguments: list[str], repository: Path) -> AllStyleUnits | PilotUnits:
    if not arguments:
        return AllStyleUnits()
    manifests = {
        path.relative_to(repository).as_posix(): path.read_text()
        for path in repository.rglob("Cargo.toml") if path.is_file()
    }
    packages = {member.package_name for member in rules.workspace_members(manifests)}
    units: list[PilotUnit] = []
    seen: set[str] = set()
    for argument in arguments:
        unit, separator, selected = argument.partition("=")
        family = rules.style_family(unit)
        if isinstance(family, rules.NotStyleUnit):
            raise Refusal(f"unknown pilot unit {unit!r}")
        if family in seen:
            raise Refusal(f"duplicate pilot unit {unit!r}")
        seen.add(family)
        requested = tuple(package.strip() for package in selected.split(",")) if separator else ()
        for package in requested:
            if package not in packages:
                raise Refusal(f"unknown pilot crate {package!r}")
        units.append(PilotUnit(family, tuple(dict.fromkeys(requested))))
    return PilotUnits(tuple(units))


@dataclass(frozen=True)
class StandingStyleProduction:
    repository: Path
    name: str
    checkout: Path
    doc: Path
    branch_point: str


@dataclass(frozen=True)
class NoStyleProduction:
    repository: Path
    name: str

    @property
    def checkout(self) -> Path:
        return self.repository.parent / self.name

    @property
    def doc(self) -> Path:
        return self.checkout / "docs" / f"{self.name}-production.md"


@dataclass(frozen=True)
class StyleProduction:
    parent: Production
    repository: Path
    name: str
    branch_point: str
    branch_tip: str
    merge_branch: str
    checkout: Path
    doc: Path
    proposals: str
    merge_tests: tuple[str, ...]

    @property
    def log(self) -> Path:
        return (Path(production_field(self.doc.read_text().splitlines(), "Log")) if self.doc.is_file()
                else Path("docs") / f"{self.name}-log.md")


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def standing_style_production(repo_dir: Path) -> StandingStyleProduction | NoStyleProduction:
    """Locate the repository's standing production without changing any state."""
    repository = Path(_git(repo_dir, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    trees = tuple(Path(line.removeprefix("worktree ")) for line in
                  _git(repository, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree "))
    for row in staff.snapshot().find(kind="showrunner"):
        if not isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) or not row.doc.path.is_file():
            continue
        doc = row.doc.path
        checkout = next((tree for tree in trees if doc.is_relative_to(tree)), None)
        if checkout is None:
            continue
        lines = doc.read_text().splitlines()
        if optional_field(lines, "Kind") == FieldPresent("style"):
            branch_point = production_field(lines, "Branch point").removeprefix("refs/heads/")
            return StandingStyleProduction(repository, row.name, checkout, doc, branch_point)
    return NoStyleProduction(repository, f"{repository.name}-style")


def _request(name: str) -> StyleProduction:
    parent = read_production(name)
    lines = parent.doc.read_text().splitlines()
    proposals = production_field(lines, "Proposals")
    declared_repo = Path(production_field(lines, "Repository")).expanduser().resolve()
    standing = standing_style_production(declared_repo)
    repository, name = standing.repository, standing.name
    branch_point = parent.merge_branch.removeprefix("refs/heads/")
    _ = _git(repository, "check-ref-format", f"refs/heads/{branch_point}")
    branch_tip = _git(repository, "rev-parse", "--verify", f"refs/heads/{branch_point}^{{commit}}")
    return StyleProduction(
        parent, repository, name, branch_point, branch_tip,
        "style/" + branch_point.replace("/", "-"), standing.checkout,
        standing.doc, proposals,
        tuple(line for line in lines if line.startswith("- **Merge tests:** ")),
    )


def _check_existing(style: StyleProduction) -> bool:
    standing = standing_style_production(style.repository)
    if isinstance(standing, StandingStyleProduction):
        existing = read_production(standing.name)
        lines = existing.doc.read_text().splitlines()
        branch_point = standing.branch_point
        if branch_point != style.branch_point:
            raise StyleBranchPointTaken(
                f"{style.repository.name} already has style production {style.name} on branch point {branch_point}; "
                + "one branch point per repository",
            )
        if production_field(lines, "Kind") != "style" or existing.merge_branch != style.merge_branch:
            raise Refusal(f"{style.doc}: does not describe {style.name} on {style.merge_branch}")
    if (style.checkout / ".git").exists():
        common = _git(style.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common != _git(style.repository, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise Refusal(f"{style.checkout}: belongs to another repository")
        if _git(style.checkout, "symbolic-ref", "--short", "HEAD") != style.merge_branch:
            raise Refusal(f"{style.checkout}: must be on {style.merge_branch}")
    elif style.checkout.exists() and any(style.checkout.iterdir()):
        raise Refusal(f"{style.checkout}: is not an empty directory or a style worktree")
    return style.doc.exists() and style.name in showrunners.timer_names()


def _production_text(style: StyleProduction) -> str:
    return "\n".join([
        f"# Production — {style.name}", "",
        "> **Status: PRODUCTION — running.** Standing style coverage and mechanical fixes.", "",
        "## Production Context", "",
        f"- **Repository:** {style.repository}",
        f"- **Merge branch:** {style.merge_branch}",
        "- **Kind:** style",
        f"- **Branch point:** {style.branch_point}",
        f"- **Showrunner checkout:** {style.checkout}",
        f"- **Log:** {style.log}",
        f"- **Proposals:** {style.proposals}",
        "- **Updates:** on demand", "- **Push:** git", *style.merge_tests, "",
    ])


def _plan_text(style: StyleProduction, family: rules.Family, catalog: tuple[rules.Rule, ...]) -> str:
    family_rules = [f"  - `{rule.rule_id}` — tier {rule.tier}" for rule in catalog if rule.family == family]
    return "\n".join([
        f"# {style.name} — {family}", "",
        "> **Standing:** style_plan.py", "", "## Delegation Context", "",
        f"- **Project:** {style.repository.name} — standing {family} style coverage",
        f"- **Repository:** {style.repository}",
        "- **Stack:** Rust; Python style ledger and ticket planner",
        "- **Layout:** Ticket files are selected at refill; this unit owns no files.",
        "- **Key files:** `~/.claude/docs/style_production.md` — standing procedure, yielding and mechanical criteria",
        "- **Test lanes:** The touched packages' unit and integration tests.",
        "- **Build:** For each touched package: `bash ~/.claude/scripts/direct/verify.sh check <package>`",
        "- **Test:** For each touched package: `bash ~/.claude/scripts/direct/verify.sh test <package>`",
        "- **Lint:** For each touched package: `bash ~/.claude/scripts/direct/verify.sh lint <package>`",
        f"- **Family:** {family}; current rules: `~/.claude/scripts/lib/py ~/.claude/scripts/style/rules.py list --repo {style.checkout}`",
        *family_rules,
        "- **Invariants:** Follow `~/.claude/docs/style_production.md`. Product work wins; skip reserved, unmerged or dirty files. "
        + "Apply only mechanical fixes under its criteria; hold every other finding as a proposal. "
        + "Resume the open ticket before refill. The ledger is the record; never commit or shrink this standing plan.",
        "", "## Phases", "",
    ])


def _write_missing(path: Path, text: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, text)


def _configure_pilot(style: StyleProduction, pilot: PilotUnits) -> None:
    selected = {unit.family: unit for unit in pilot.units}
    launched = {
        family for family in FAMILIES
        if (style.repository.parent / f"{style.name}-{family}").exists()
    }
    units = ", ".join(f"{style.name}-{family}" for family in FAMILIES if family in launched or family in selected)
    _set_fields(style, {"Pilot units": units})
    for family in FAMILIES:
        if family in launched:
            continue
        path = style.checkout / PLAN_DIRECTORY / f"{style.name}-{family}-unit.md"
        original = path.read_text()
        plan = original.splitlines(keepends=True)
        context = plan.index("## Delegation Context\n") + 1
        stop = next((index for index in range(context, len(plan)) if plan[index].startswith("## ")), len(plan))
        plan[context:stop] = [line for line in plan[context:stop] if not line.lstrip("- ").startswith("**Pilot scope:**")]
        unit = selected.get(family)
        if unit is not None and unit.packages:
            family_line = next(index for index in range(context, len(plan)) if plan[index].startswith("- **Family:**"))
            insertion = family_line + 1
            while insertion < len(plan) and plan[insertion].startswith("  - "):
                insertion += 1
            plan.insert(insertion, "- **Pilot scope:** " + ", ".join(unit.packages) + "\n")
        text = "".join(plan)
        if text != original:
            write_text_atomic(path, text)


def _pilot_steps(steps: tuple[str, ...], pilot: AllStyleUnits | PilotUnits, name: str) -> tuple[str, ...]:
    if isinstance(pilot, AllStyleUnits):
        units = ", ".join(f"{name}-{family}" for family in FAMILIES)
        return (*steps[:4], steps[4] + f"; units {units}", *steps[5:])
    units = "; ".join(f"{name}-{unit.family}" for unit in pilot.units)
    scopes = "; ".join(
        f"{name}-{unit.family}: {', '.join(unit.packages) if unit.packages else 'all crates'}"
        for unit in pilot.units
    )
    return (*steps[:3], steps[3] + f"; pilot units {units}",
            steps[4] + f"; pilot scopes {scopes}; preserve launched units", *steps[5:])


def _exclude(style: StyleProduction) -> None:
    patterns = ("/docs/style-production/", f"/docs/{style.doc.name}", f"/{style.log.as_posix()}")
    for pattern in patterns:
        ensure_excluded(style.repository, pattern)


def _run_script(script: Path, *arguments: str) -> None:
    result = subprocess.run([str(SCRIPTS / "lib/py"), str(script), *arguments], check=False)
    if result.returncode:
        raise Refusal(f"{script.name} exited {result.returncode}; rerun add_style to continue")


def _open_session_guidance(style: StyleProduction, reason: str) -> str:
    attach = herdr.attach_hint(style.name, style.name)
    return (
        f"{style.name} was left open by add_showrunner.py ({reason}); look at it with {attach}; "
        + "once it has its update timer, rerun add_style, which attaches it; "
        + "if it never gets one, end that session and rerun"
    )


def _attach_parent(style: StyleProduction) -> None:
    current_parent = showrunner_tree.parent_of(style.name)
    if current_parent != style.parent.name:
        if current_parent:
            _run_script(SHOWRUNNERS, "detach", style.name)
        _run_script(SHOWRUNNERS, "attach", style.name, style.parent.name)


def _attach(style: StyleProduction) -> None:
    _attach_parent(style)
    _update_destination(style)
    _run_script(SCRIPTS / "style/style_sync.py", "resume", "--repo", str(style.checkout),
                "--production", style.name, "--branch-point", style.branch_point,
                "--style-branch", style.merge_branch)


def _update_destination(style: StyleProduction) -> None:
    _set_fields(style, {"Proposals": style.proposals})


def _set_fields(style: StyleProduction, replacements: dict[str, str]) -> None:
    previous = style.doc.read_text()
    lines = previous.splitlines()
    for field, value in replacements.items():
        prefix = f"- **{field}:** "
        for index, line in enumerate(lines):
            if line.startswith(prefix):
                lines[index] = prefix + value
                break
        else:
            lines.insert(lines.index("## Production Context") + 1, prefix + value)
    updated = "\n".join(lines) + "\n"
    if updated != previous:
        write_text_atomic(style.doc, updated)


def _steps(style: StyleProduction, existing: bool) -> tuple[str, ...]:
    if existing:
        return (
            f"read {style.parent.doc}; repository {style.repository}; Proposals {style.proposals}",
            f"attach standing {style.name} under {style.parent.name}; branch point {style.branch_point}",
            *((f"skipped: {style.name} already exists",) * 4),
            "update Proposals, run style_sync.py resume; synced tip unchanged",
        )
    return (
        f"read {style.parent.doc}; repository {style.repository}; Proposals {style.proposals}",
        f"find standing {style.name} under {style.parent.name}; branch point {style.branch_point}",
        f"ensure {style.merge_branch} from {style.branch_tip} and worktree {style.checkout}",
        (f"set Proposals in existing production doc {style.doc}"
         if style.doc.exists() else f"write missing production doc {style.doc}"),
        f"write four missing standing plans under {style.checkout / PLAN_DIRECTORY}",
        f"exclude docs/style-production/, {style.doc.relative_to(style.checkout)} and {style.log} in shared info/exclude",
        f"initialize ledger at {style.branch_tip} with {style.merge_branch}; launch via add_showrunner.py",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("--model", required=True)
    _ = parser.add_argument("--effort", required=True)
    _ = parser.add_argument("--check", action="store_true")
    _ = parser.add_argument("--pilot", action="append", default=[], metavar="UNIT[=CRATE,...]")
    args = parser.parse_args(argv)
    try:
        style = _request(cast(str, args.production))
        existing = _check_existing(style)
        pilot_arguments = cast(list[str], args.pilot)
        if existing and pilot_arguments:
            raise Refusal(f"--pilot applies only while creating {style.name}")
        pilot = _pilot_units(pilot_arguments, style.repository)
        catalog = rules.load_rules(style.repository)
        if cast(bool, args.check):
            steps = _steps(style, existing)
            if not existing:
                steps = _pilot_steps(steps, pilot, style.name)
            for number, step in enumerate(steps, 1):
                print(f"{number}. {step}")
            return 0
        if existing:
            _attach(style)
            return 0
        if style.name not in showrunners.timer_names() and herdr_tab_open(style.name, NoEndingTab()):
            raise Refusal(_open_session_guidance(style, "live session without an update timer"))
        if not (style.checkout / ".git").exists():
            present = subprocess.run(
                ["git", "-C", str(style.repository), "show-ref", "--verify", "--quiet", f"refs/heads/{style.merge_branch}"],
                check=False, capture_output=True, text=True,
            )
            if present.returncode == 1:
                _ = _git(style.repository, "branch", style.merge_branch, style.branch_tip)
            elif present.returncode:
                raise Refusal(present.stderr.strip())
            _ = _git(style.repository, "worktree", "add", str(style.checkout), style.merge_branch)
        _write_missing(style.doc, _production_text(style))
        _update_destination(style)
        for family in FAMILIES:
            _write_missing(style.checkout / PLAN_DIRECTORY / f"{style.name}-{family}-unit.md", _plan_text(style, family, catalog))
        if isinstance(pilot, PilotUnits):
            _configure_pilot(style, pilot)
        _exclude(style)
        style_tip = _git(style.repository, "rev-parse", "--verify", f"refs/heads/{style.merge_branch}^{{commit}}")
        with style_ledger.open_ledger(style.checkout) as ledger:
            ledger.ensure_synced_tip(style_tip, style.merge_branch)
        if isinstance(standing_style_production(style.repository), NoStyleProduction):
            production = read_new_production(style.doc, style.name)
            doc_options = ["--doc", str(production.doc)]
        else:
            production = read_production(style.name)
            doc_options = []
        result = subprocess.run([
            str(SCRIPTS / "lib/py"), str(LAUNCHER), "--production", production.name, *doc_options,
            "--model", cast(str, args.model), "--effort", cast(str, args.effort),
        ], check=False)
        if result.returncode in (1, 3):
            print("add_style: " + _open_session_guidance(style, f"exit {result.returncode}"), file=sys.stderr)
            return result.returncode
        if result.returncode:
            raise Refusal(f"{LAUNCHER.name} exited {result.returncode}; rerun add_style to continue")
        _attach_parent(style)
        return 0
    except (Refusal, rules.RuleValidationError, style_ledger.LedgerError, OSError, ValueError, sqlite3.Error) as error:
        print("add_style: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
