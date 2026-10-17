#!/usr/bin/env python3
"""Create or attach one standing style production per repository."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

import showrunners
from add_showrunner import herdr_session_running
from add_unit import Production, read_production
from production_units import Refusal, production_field

import rules
import style_ledger

FAMILIES: tuple[rules.Family, ...] = ("names", "values", "types", "modules")
PLAN_DIRECTORY = Path("docs/style-production/units")
LAUNCHER = Path(os.environ.get("ADD_STYLE_LAUNCHER") or SCRIPTS / "production/add_showrunner.py")
SHOWRUNNERS = Path(os.environ.get("ADD_STYLE_SHOWRUNNERS") or SCRIPTS / "production/showrunners.py")


class StyleBranchPointTaken(Refusal):
    """The repository's standing production already follows another branch point."""


@dataclass(frozen=True)
class StyleProduction:
    parent: Production
    repository: Path
    slug: str
    branch_point: str
    branch_tip: str
    merge_branch: str
    checkout: Path
    doc: Path
    proposals: str
    merge_tests: tuple[str, ...]

    @property
    def log(self) -> Path:
        return Path("docs") / f"{self.slug}-log.md"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def _request(doc: Path) -> StyleProduction:
    parent = read_production(doc)
    lines = parent.doc.read_text().splitlines()
    proposals = production_field(lines, "Proposals")
    declared_repo = Path(production_field(lines, "Repository")).expanduser().resolve()
    common = Path(_git(declared_repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    repository = common.parent
    slug = f"{repository.name}-style"
    branch_point = parent.merge_branch.removeprefix("refs/heads/")
    _ = _git(repository, "check-ref-format", f"refs/heads/{branch_point}")
    branch_tip = _git(repository, "rev-parse", "--verify", f"refs/heads/{branch_point}^{{commit}}")
    checkout = repository.parent / slug
    return StyleProduction(
        parent, repository, slug, branch_point, branch_tip,
        "style/" + branch_point.replace("/", "-"), checkout,
        checkout / "docs" / f"{slug}-production.md", proposals,
        tuple(line for line in lines if line.startswith("- **Merge tests:** ")),
    )


def _check_existing(style: StyleProduction) -> bool:
    if style.doc.exists():
        existing = read_production(style.doc)
        lines = existing.doc.read_text().splitlines()
        branch_point = production_field(lines, "Branch point").removeprefix("refs/heads/")
        if branch_point != style.branch_point:
            raise StyleBranchPointTaken(
                f"{style.repository.name} already has style production {style.slug} on branch point {branch_point}; "
                + "one branch point per repository",
            )
        if production_field(lines, "Kind") != "style" or existing.merge_branch != style.merge_branch:
            raise Refusal(f"{style.doc}: does not describe {style.slug} on {style.merge_branch}")
    if (style.checkout / ".git").exists():
        common = _git(style.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common != _git(style.repository, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise Refusal(f"{style.checkout}: belongs to another repository")
        if _git(style.checkout, "symbolic-ref", "--short", "HEAD") != style.merge_branch:
            raise Refusal(f"{style.checkout}: must be on {style.merge_branch}")
    elif style.checkout.exists() and any(style.checkout.iterdir()):
        raise Refusal(f"{style.checkout}: is not an empty directory or a style worktree")
    return style.doc.exists() and style.slug in showrunners.timer_slugs()


def _production_text(style: StyleProduction) -> str:
    rows = [
        f"| {family}-unit | {PLAN_DIRECTORY}/{family}-unit.md | {style.repository.parent / (style.slug + '-' + family)} "
        + f"| {style.slug}-{family} | — | none — takes what the board leaves free |"
        for family in FAMILIES
    ]
    return "\n".join([
        f"# Production — {style.slug}", "",
        "> **Status: PRODUCTION — running.** Standing style coverage and mechanical fixes.", "",
        "## Production Context", "",
        f"- **Repository:** {style.repository}",
        f"- **Merge branch:** {style.merge_branch}",
        "- **Kind:** style",
        f"- **Branch point:** {style.branch_point}",
        f"- **Parent production:** {style.parent.doc}",
        f"- **Showrunner checkout:** {style.checkout}",
        f"- **Log:** {style.log}",
        f"- **Proposals:** {style.proposals}",
        "- **Updates:** on demand", "- **Push:** git", *style.merge_tests, "",
        "## Units", "",
        "| Unit | Plan | Worktree | Branch | Port | Owns |",
        "| --- | --- | --- | --- | --- | --- |", *rows, "",
    ])


def _plan_text(style: StyleProduction, family: rules.Family, catalog: tuple[rules.Rule, ...]) -> str:
    family_rules = [f"  - `{rule.rule_id}` — tier {rule.tier}" for rule in catalog if rule.family == family]
    return "\n".join([
        f"# {style.slug} — {family}", "",
        f"> **Production: {style.slug}** — unit `{family}-unit`; production doc `{style.doc}`",
        "> **Standing:** style_plan.py", "", "## Delegation Context", "",
        f"- **Project:** {style.repository.name} — standing {family} style coverage",
        f"- **Repository:** {style.repository}",
        "- **Stack:** Rust; Python style ledger and ticket planner",
        "- **Layout:** Ticket files are selected at refill; this unit owns no files.",
        "- **Key files:** `~/.claude/docs/style_production.md` — standing procedure, yielding and mechanical criteria",
        "- **Test lanes:** The touched packages' unit and integration tests.",
        "- **Build:** For each touched package: `bash ~/.claude/scripts/delegate/verify.sh check <package>`",
        "- **Test:** For each touched package: `bash ~/.claude/scripts/delegate/verify.sh test <package>`",
        "- **Lint:** For each touched package: `bash ~/.claude/scripts/delegate/verify.sh lint <package>`",
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
        _ = path.write_text(text)


def _exclude(style: StyleProduction) -> None:
    path = Path(_git(style.repository, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    previous = path.read_text() if path.exists() else ""
    patterns = ("/docs/style-production/", f"/docs/{style.doc.name}", f"/{style.log.as_posix()}")
    missing = [pattern for pattern in patterns if pattern not in previous.splitlines()]
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(previous + ("\n" if previous and not previous.endswith("\n") else "") + "\n".join(missing) + "\n")


def _run_script(script: Path, *arguments: str) -> None:
    result = subprocess.run([str(SCRIPTS / "lib/py"), str(script), *arguments], check=False)
    if result.returncode:
        raise Refusal(f"{script.name} exited {result.returncode}; rerun add_style to continue")


def _open_session_guidance(slug: str, reason: str) -> str:
    return (
        f"{slug} was left open by add_showrunner.py ({reason}); look at it with herdr session attach {slug}; "
        + "once it has its update timer, rerun add_style, which attaches it; "
        + "if it never gets one, end that session and rerun"
    )


def _attach(style: StyleProduction) -> None:
    current_parent = showrunners.parent_of(style.slug)
    if current_parent != style.parent.slug:
        if current_parent:
            _run_script(SHOWRUNNERS, "detach", style.slug)
        _run_script(SHOWRUNNERS, "attach", style.slug, style.parent.slug)
    previous = style.doc.read_text()
    replacements = {"Parent production": str(style.parent.doc), "Proposals": style.proposals}
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
        _ = style.doc.write_text(updated)
    with style_ledger.open_ledger(style.checkout) as ledger:
        for handoff in ledger.pending_handoffs():
            print(json.dumps(handoff, sort_keys=True))


def _steps(style: StyleProduction, existing: bool) -> tuple[str, ...]:
    if existing:
        return (
            f"read {style.parent.doc}; repository {style.repository}; Proposals {style.proposals}",
            f"attach standing {style.slug} under {style.parent.slug}; branch point {style.branch_point}",
            *((f"skipped: {style.slug} already exists",) * 4),
            "update Parent production and Proposals, print pending hand-offs; synced tip unchanged",
        )
    return (
        f"read {style.parent.doc}; repository {style.repository}; Proposals {style.proposals}",
        f"find standing {style.slug} under {style.parent.slug}; branch point {style.branch_point}",
        f"ensure {style.merge_branch} from {style.branch_tip} and worktree {style.checkout}",
        f"write missing production doc {style.doc}",
        f"write four missing standing plans under {style.checkout / PLAN_DIRECTORY}",
        f"exclude docs/style-production/, {style.doc.relative_to(style.checkout)} and {style.log} in shared info/exclude",
        f"initialize ledger at {style.branch_tip} with {style.merge_branch}; launch via add_showrunner.py",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--production", required=True, type=Path)
    _ = parser.add_argument("--model", required=True)
    _ = parser.add_argument("--effort", required=True)
    _ = parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        style = _request(cast(Path, args.production))
        existing = _check_existing(style)
        catalog = rules.load_rules(style.repository)
        if cast(bool, args.check):
            for number, step in enumerate(_steps(style, existing), 1):
                print(f"{number}. {step}")
            return 0
        if existing:
            _attach(style)
            return 0
        if style.slug not in showrunners.timer_slugs() and herdr_session_running(style.slug):
            raise Refusal(_open_session_guidance(style.slug, "live session without an update timer"))
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
        for family in FAMILIES:
            _write_missing(style.checkout / PLAN_DIRECTORY / f"{family}-unit.md", _plan_text(style, family, catalog))
        _exclude(style)
        with style_ledger.open_ledger(style.checkout) as ledger:
            try:
                _ = ledger.synced_at()
            except style_ledger.LedgerError:
                tip = style.branch_tip
            else:
                tip = ledger.summary().synced_tip
            ledger.set_tip(tip, style.merge_branch)
        result = subprocess.run([
            str(SCRIPTS / "lib/py"), str(LAUNCHER), "--production", str(style.doc),
            "--model", cast(str, args.model), "--effort", cast(str, args.effort),
        ], check=False)
        if result.returncode in (1, 3):
            print("add_style: " + _open_session_guidance(style.slug, f"exit {result.returncode}"), file=sys.stderr)
            return result.returncode
        if result.returncode:
            raise Refusal(f"{LAUNCHER.name} exited {result.returncode}; rerun add_style to continue")
        return 0
    except (Refusal, rules.RuleValidationError, style_ledger.LedgerError, OSError, ValueError, sqlite3.Error) as error:
        print("add_style: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
