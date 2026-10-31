#!/usr/bin/env python3
"""File style decisions and track their answers and builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import UnionType
from typing import Protocol, cast
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

import coverage_report
import rules
import style_footprint
import style_ledger
import style_rules
from approved_renames import (ApprovedRename, BindingRename, BindingScope, NoAppliedCheck, RenameNotApplied,
                              approved_rename, binding_rename, BindingScopeReader)
from add_style import NoStyleProduction, StandingStyleProduction, standing_style_production
from declarations import declaration_line
from issues import Issue, frontmatter, open_issues, plain
from production_units import cell_value, plan_cell_is_retired, unit_table
from proposal_issue import (GOAL, INHERITED_FIELDS, STAR, ApprovedDecision, DeclinedDecision, Refusal,
                            UnansweredDecision, approve_unanswered, decision_answer, fill_ranking, refresh_decision, require_generated_body)
from unit_files import file_text, git, unit_file_holders
from unit_plans import PilotScope, crate_in_scope, family_pilot_scopes
from process_failure import failure_detail
from user_zone import user_zone


def proposal_command(*arguments: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "production/proposal_issue.py"), *arguments],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise style_ledger.LedgerError(failure_detail(result))


def renumber() -> None:
    result = subprocess.run([sys.executable, str(SCRIPTS / "prioritize/renumber.py"), "--apply"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise style_ledger.LedgerError(failure_detail(result))


def is_decision(issue: Issue) -> bool:
    return (all(issue.value(key) for key in ("style_rule", "style_crate", "style_rule_blob"))
            and {"proposal", "style"} <= set(issue.fields.get("tags", [])))


def approved_decisions(folder: Path, unit: str) -> tuple[Issue, ...]:
    return tuple(sorted((issue for issue in open_issues(folder, "style")
                         if is_decision(issue) and isinstance(decision_answer(issue), ApprovedDecision)
                         and issue.value("unit") == unit),
                        key=lambda issue: (issue.value("date_created"), issue.path.name)))


def issue_findings(ledger: style_ledger.Ledger, issue: Path) -> tuple[style_ledger.HeldFinding, ...]:
    """The findings the issue shows: each finding filed to it, but not one a newer verdict withdrew."""
    return tuple(finding for finding in ledger.proposal_findings(issue)
                 if not isinstance(finding.state, style_ledger.Withdrawn))


def left_to_build(findings: tuple[style_ledger.HeldFinding, ...]) -> tuple[style_ledger.HeldFinding, ...]:
    """The findings that are not built or waived. issue_findings already leaves out the withdrawn ones."""
    return tuple(finding for finding in findings
                 if not isinstance(finding.state, (style_ledger.Built, style_ledger.Waived)))


def bare_duplicates(candidates: tuple[style_ledger.HeldFinding, ...],
                    holders: tuple[style_ledger.HeldFinding, ...]) -> dict[str, str]:
    """The qualified finding in holders that each bare duplicate in candidates duplicates, by finding id.

    A bare duplicate is a rename whose before has no `::` and whose before and after are the old and new name of a
    qualified rename in the same file under the same rule; the parent rule and its `#uncovered` part count as one.
    When several match, the nearest line wins, then the lower finding id. Only filed and built findings count: the
    ledger withdraws no other state, and a waived finding is not built.
    """
    pairs = [(finding, pair) for finding in holders if isinstance(finding.state, (style_ledger.Filed, style_ledger.Built))
             if isinstance(pair := approved_rename(finding.kind, finding.before, finding.after), ApprovedRename)]
    duplicates: dict[str, str] = {}
    for bare in candidates:
        if (bare.kind != "rename" or "::" in bare.before
                or not isinstance(bare.state, (style_ledger.Filed, style_ledger.Built))):
            continue
        matches = [finding for finding, pair in pairs
                   if (pair.old, pair.new, finding.path) == (bare.before, bare.after, bare.path)
                   and finding.rule.removesuffix("#uncovered") == bare.rule.removesuffix("#uncovered")]
        if matches:
            duplicates[bare.finding_id] = min(matches, key=lambda item: (abs(item.line - bare.line), item.finding_id)).finding_id
    return duplicates


def duplicate_line(duplicate: style_ledger.BareDuplicate) -> str:
    return f"drop {duplicate.finding_id} in {duplicate.decision}: duplicate of {duplicate.qualified}"


def repository_name(repo: Path) -> str:
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise style_ledger.LedgerError(result.stderr.strip())
    return Path(result.stdout.strip()).parent.name


def style_slug(repo: Path) -> str:
    return repository_name(repo) + "-style"


def workspace_directories(ledger: style_ledger.Ledger) -> tuple[str, ...]:
    return tuple(member.directory for member in coverage_report.workspace(ledger, ledger.synced_commit()))


def crate_for(directories: tuple[str, ...], path: str) -> str:
    # A finding names a site even when its coverage cell is the enclosing crate.
    return next((directory for directory in sorted(directories, key=len, reverse=True)
                 if path == directory or path.startswith(directory + "/")), ".")


@dataclass(frozen=True)
class IssueProject:
    short: str
    project: str


def issue_project(repo: Path, crate: str, vault: Path) -> IssueProject:
    name = repository_name(repo)
    manifest = repo / crate / "Cargo.toml"
    data = cast(dict[str, object], tomllib.loads(manifest.read_text()))
    package = cast(dict[str, object], data.get("package", {}))
    projects: dict[str, IssueProject] = {}
    for base in sorted((vault / "home-base").glob("issues - *.base")):
        match = re.search(r'project\.contains\(link\("([^"\n]+)"\)\)', base.read_text())
        if match:
            project = match.group(1)
            _ = projects.setdefault(project, IssueProject(base.stem.removeprefix("issues - "), project))
    package_name = package.get("name")
    if isinstance(package_name, str) and package_name in projects:
        return projects[package_name]
    if name in projects:
        return projects[name]
    print(f"no issue base for {name}: run /new_issue_base {name} in hanadocs")
    return IssueProject(name, name)


@dataclass(frozen=True, order=True)
class DecisionKey:
    rule: str
    crate: str
    blob: str


def decision_key(finding: style_ledger.HeldFinding, directories: tuple[str, ...]) -> DecisionKey:
    return DecisionKey(finding.rule.removesuffix("#uncovered"), crate_for(directories, finding.path), finding.rule_blob)


UNSAFE_NAME = re.compile(r"[^\w .-]")
UNSAFE_CRATE_LABEL = re.compile(r"[^\w.+-]")
PLAIN_CRATE = re.compile(r"[A-Za-z0-9_./-]+")


def batch_name(key: DecisionKey) -> str:
    label = UNSAFE_NAME.sub("-", PurePosixPath(key.rule).stem).strip(" .-") or "rule"
    # The root crate has no crate part, so no crate path can give its name.
    if key.crate != ".":
        # A plain crate path shows each "/" as "+". A plain path holds no "+", so two plain paths give two labels.
        shown = key.crate.replace("/", "+")
        crate = UNSAFE_CRATE_LABEL.sub("-", shown).strip(".-")
        # Any other path can give the label of a plain path, so it adds a hash of the path.
        # A plain label cannot hold the parentheses, so no plain path gives a hashed name.
        plain_path = PLAIN_CRATE.fullmatch(key.crate) is not None and crate == shown
        label += f" {crate}" if plain_path else f" {crate} ({hashlib.sha256(key.crate.encode()).hexdigest()[:8]})"
    return f"style {label} {key.blob[:8]}.md"


DEFAULT_GOAL = "1 - Ship Hana"


def project_goal(proposals: Path, project: str) -> str:
    counts: Counter[str] = Counter()
    for path in sorted(proposals.glob("*.md")):
        issue = Issue(path, frontmatter(path))
        goal = plain(issue.value("backlog_goal"))
        if (issue.value("status") == "open" and "style" not in issue.fields.get("tags", [])
                and plain(issue.value("project")) == plain(project) and GOAL.fullmatch(goal)):
            counts[goal] += 1
    if counts:
        return min(counts, key=lambda goal: (-counts[goal], int(goal.split(" - ", 1)[0]), goal))
    print(f"goal: no ranked {project} issue; default {DEFAULT_GOAL}")
    return DEFAULT_GOAL


def proposed_ranking(kind: style_ledger.FindingKind, finding_count: int, goal: str) -> str:
    effort = 1 if finding_count <= 3 else 2 if finding_count <= 15 else 3
    impact = 1 if kind == "rename" else 2
    return (f"goal {goal} · alignment {STAR * 2} · impact {STAR * impact} "
            + f"· urgency {STAR} · effort {STAR * effort}")


def proposal_kind(finding: style_ledger.HeldFinding, definition: rules.Rule) -> style_ledger.FindingKind:
    return "restructure" if definition.tier == "D" or definition.mode_propose else finding.kind


@dataclass(frozen=True)
class GitHubRuleRoot:
    repository: str
    branch: str

    def url(self, path: str) -> str:
        return f"https://github.com/{self.repository}/blob/{quote(self.branch, safe='')}/{quote(path, safe='/')}"


def github_rule_root(root: Path) -> GitHubRuleRoot:
    remote = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                            capture_output=True, text=True, check=False)
    if remote.returncode:
        raise style_ledger.LedgerError(f"{root}: cannot read origin: {remote.stderr.strip()}")
    origin = remote.stdout.strip()
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([^/\s]+/[^/\s]+)", origin)
    if match is None:
        raise style_ledger.LedgerError(f"{root}: non-GitHub origin {origin}")
    branch = subprocess.run(["git", "-C", str(root), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                            capture_output=True, text=True, check=False)
    name = branch.stdout.strip().removeprefix("origin/")
    if branch.returncode or not name:
        name = "main"
        print(f"rules: {root}: no origin/HEAD; default main")
    return GitHubRuleRoot(match[1].removesuffix(".git"), name)


@dataclass
class RuleLinker:
    repo: Path
    roots: dict[Path, GitHubRuleRoot] = field(default_factory=dict)

    def rule_url(self, rule: str) -> str:
        origin, separator, path = rule.removesuffix("#uncovered").partition(":")
        if not separator or origin not in {"global", "local"} or not path:
            raise style_ledger.LedgerError(f"invalid rule {rule}")
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise style_ledger.LedgerError(f"invalid rule path {rule}")
        root = (style_rules.NATE_STYLE_DIR if origin == "global" else self.repo).resolve()
        if root not in self.roots:
            self.roots[root] = github_rule_root(root)
        return self.roots[root].url(path)

    def urls(self, findings: tuple[style_ledger.HeldFinding, ...]) -> tuple[str, ...]:
        return tuple(sorted({self.rule_url(finding.rule) for finding in findings}))


def link_rules(ledger: style_ledger.Ledger, proposals: Path) -> None:
    linker = RuleLinker(ledger.repo)
    linked = already = skipped = 0
    for path in sorted(proposals.glob("*.md")):
        issue = Issue(path, frontmatter(path))
        if not is_decision(issue):
            continue
        if "style_rules" in issue.fields:
            already += 1
            continue
        findings = issue_findings(ledger, path)
        if not findings:
            skipped += 1
            print(f"skipped {path}: no decision findings")
            continue
        proposal_command("rules", str(path), "--rules", ",".join(linker.urls(findings)))
        linked += 1
    print(f"linked {linked}, already linked {already}, skipped {skipped}")


def decision_kind(findings: tuple[style_ledger.HeldFinding, ...],
                  definitions: Mapping[str, rules.Rule]) -> style_ledger.FindingKind:
    kinds = {proposal_kind(item, definitions[item.rule.removesuffix("#uncovered")])
             if item.rule.removesuffix("#uncovered") in definitions else item.kind for item in findings}
    return "restructure" if "restructure" in kinds else "type" if "type" in kinds else "rename"


@dataclass(frozen=True)
class FamilyUnit:
    """The live unit of the standing style production that builds one family's decisions, with its Plan cell."""
    unit: str
    plan: str


def family_units(doc: Path) -> dict[rules.Family, FamilyUnit]:
    units: dict[rules.Family, FamilyUnit] = {}
    for row in unit_table(doc.read_text().splitlines()):
        family = rules.style_family(cell_value(row["Unit"]))
        if not isinstance(family, rules.NotStyleUnit) and not plan_cell_is_retired(row.get("Plan", "")):
            units[family] = FamilyUnit(cell_value(row["Unit"]), cell_value(row["Plan"]))
    return units


@dataclass
class DecisionWriter:
    """What one command run loads once and shares for every decision it writes."""
    ledger: style_ledger.Ledger
    proposals: Path
    parent: str
    definitions: Mapping[str, rules.Rule]
    holders: Mapping[str, frozenset[str]]
    linker: RuleLinker
    repository: str
    standing: StandingStyleProduction | NoStyleProduction
    goals: dict[str, str] = field(default_factory=dict)

    def goal(self, project: str) -> str:
        if project not in self.goals:
            self.goals[project] = project_goal(self.proposals, project)
        return self.goals[project]

    def text(self, key: DecisionKey, findings: tuple[style_ledger.HeldFinding, ...]) -> str:
        crate = key.crate if key.crate != "." else self.repository
        stem = PurePosixPath(key.rule).stem
        body = [f"# Style decision: {stem} in {crate}", "", f"Parent: {self.parent}", "",
                f"Kind: {decision_kind(findings, self.definitions)}", f"Rule: {key.rule}",
                f"Rule link: {self.linker.rule_url(key.rule)}",
                f"Findings: {len(findings)}; files: {len({finding.path for finding in findings})}", "",
                style_ledger.FINDING_REPLACEMENT_GUIDANCE, ""]
        seen: set[str] = set()
        for finding in sorted(findings, key=lambda item: (len(item.before) + len(item.after), item.path, item.line, item.finding_id)):
            if finding.path in seen:
                continue
            seen.add(finding.path)
            live = ", ".join(sorted(unit for unit, paths in self.holders.items() if finding.path in paths)) or "none"
            body.extend([f"## Example {len(seen)}", f"Site: {finding.path}:{finding.line}",
                         f"Live units holding file: {live}", "Before:", "```", finding.before, "```",
                         "After:", "```", finding.after, "```", ""])
            if len(seen) == 3:
                break
        body.append("Recommended answer: approve the described changes under this rule text.")
        return "\n".join(body) + "\n"

    def rule_approval(self, key: DecisionKey, inherited: Mapping[str, str]) -> dict[str, str]:
        """The approval fields of a rule that is always approved, as a user approval writes them, for a new decision
        that inherits no approval. Refuse when no live unit of the rule's family builds it."""
        definition = self.definitions.get(key.rule)
        if (definition is None or not isinstance(definition.approval, rules.AlwaysApproved)
                or isinstance(decision_answer(Issue(Path(), {name: [value] for name, value in inherited.items()})),
                              ApprovedDecision)):
            return {}
        units = family_units(self.standing.doc) if isinstance(self.standing, StandingStyleProduction) else {}
        unit = units.get(definition.approval.family)
        if unit is None:
            raise style_ledger.LedgerError(f"rule {key.rule} is always approved, but no {definition.approval.family} unit builds it")
        slug = style_slug(self.ledger.repo)
        return {"showrunner": slug, "unit": unit.unit, "plan": unit.plan, "approved_by": f"rule {key.rule}",
                "approved_in": slug}

    def approve_by_rule(self, decision: Issue) -> None:
        """Approve an open decision still waiting for the user when its rule, whatever the blob, is always approved."""
        if not isinstance(decision_answer(decision), UnansweredDecision):
            return
        rule = decision.value("style_rule")
        approval = self.rule_approval(DecisionKey(rule, decision.value("style_crate"), decision.value("style_rule_blob")), {})
        if approval:
            # The answer is read again under the issue lock, so an approval written since the read above is kept.
            if approve_unanswered(decision.path, {name: approval[name] for name in ("showrunner", "unit", "plan")},
                                  approval["approved_by"], approval["approved_in"], datetime.now(user_zone()).date()):
                    print(f"approved {decision.path}: rule {rule} is always approved")

    def create(self, key: DecisionKey, findings: tuple[style_ledger.HeldFinding, ...],
               inherited: Mapping[str, str]) -> Path:
        issue = self.proposals / batch_name(key)
        approval = self.rule_approval(key, inherited)
        project = issue_project(self.ledger.repo, key.crate, self.proposals.parent)
        ranking = proposed_ranking(decision_kind(findings, self.definitions), len(findings), self.goal(project.project))
        with tempfile.TemporaryDirectory(prefix="style-decision-") as scratch:
            body = Path(scratch) / "body.md"
            _ = body.write_text(self.text(key, findings))
            proposal_command("create", str(issue), "--project", project.project, "--short", project.short,
                             "--proposed-by", style_slug(self.ledger.repo), "--extra-tags", "style",
                             "--rule", key.rule, "--crate", key.crate, "--rule-blob", key.blob,
                             "--count", str(len(findings)), "--body-file", str(body),
                             "--proposed-ranking", ranking, "--initial-fields", json.dumps({**inherited, **approval}),
                             "--rules", ",".join(self.linker.urls(findings)))
        if approval:
            print(f"approved {issue}: rule {key.rule} is always approved")
        return issue

    def refresh(self, decision: Issue) -> None:
        """Write the decision's count, examples and open ranking only when its bytes change."""
        key = DecisionKey(decision.value("style_rule"), decision.value("style_crate"), decision.value("style_rule_blob"))
        findings = issue_findings(self.ledger, decision.path)
        ranking = proposed_ranking(decision_kind(findings, self.definitions), len(findings),
                                   self.goal(plain(decision.value("project"))))
        _ = refresh_decision(decision.path, len(findings), self.text(key, findings), ranking,
                             datetime.now(user_zone()).date())


def decision_writer(ledger: style_ledger.Ledger, proposals: Path, parent: str) -> DecisionWriter:
    standing = standing_style_production(ledger.repo)
    holders = unit_file_holders(ledger.repo, standing.doc, ledger) if isinstance(standing, StandingStyleProduction) else {}
    return DecisionWriter(ledger, proposals, parent, {rule.rule_id: rule for rule in rules.load_rules(ledger.repo)},
                          holders, RuleLinker(ledger.repo), repository_name(ledger.repo), standing)


def find_decision(proposals: Path, key: DecisionKey) -> Path:
    issue = proposals / batch_name(key)
    if issue.exists():
        fields = frontmatter(issue)
        if any(fields.get(name) != [value] for name, value in (
            ("style_rule", key.rule), ("style_crate", key.crate), ("style_rule_blob", key.blob),
        )):
            raise style_ledger.LedgerError(f"{issue}: different decision key")
    return issue


# The close reasons that complete and the withdrawal write. A closed decision with no answer takes a later
# finding only under one of them.
BUILT_REASON = "built in "
NOTHING_LEFT_REASON = "nothing left to build: "
FINISHED_REASONS = (BUILT_REASON, NOTHING_LEFT_REASON)


@dataclass(frozen=True)
class RankWait:
    """The family's work at the file waits: a family ranked above it has open work there."""
    family: rules.Family
    path: str
    higher: rules.Family

    def line(self) -> str:
        return f"waiting {self.family}: {self.path} has open {self.higher} work"


def ranked_below_unit(family: rules.Family, crate: str, scopes: Mapping[rules.Family, PilotScope]) -> bool:
    """Whether a family ranked above family has a unit whose pilot scope holds crate."""
    return any(higher in scopes and crate_in_scope(scopes[higher], crate) for higher in rules.ranked_above(family))


@dataclass(frozen=True)
class RankedWork:
    """The families with open work at each file."""
    families: Mapping[str, frozenset[rules.Family]]

    def waits(self, family: rules.Family) -> dict[str, RankWait]:
        """Each file where family waits, with the highest family ranked above it that has open work there."""
        above = rules.ranked_above(family)
        return {path: RankWait(family, path, next(higher for higher in above if higher in present))
                for path, present in self.families.items() if not present.isdisjoint(above)}


# A cell in one of these states has work that has not landed in the synced tip.
OPEN_CELL_STATES: frozenset[style_ledger.CellState] = frozenset({"open", "stale", "awaiting merge"})


class RankExemptions:
    """Decision B's finding-level exceptions, located in the synced file, shared by one planning or filing run."""

    def __init__(self, ledger: style_ledger.Ledger, tip: str) -> None:
        self.ledger: style_ledger.Ledger = ledger
        self.tip: str = tip
        self.sources: dict[str, str] = {}
        self.binding_scopes: dict[str, BindingScopeReader] = {}
        self.test_ranges: dict[str, tuple[tuple[int, int], ...]] = {}
        self.types: dict[str, list[style_ledger.HeldFinding]] = {}
        self.types_loaded: bool = False

    def source(self, path: str) -> str:
        if path not in self.sources:
            try:
                blob = GitProposalReader(self.ledger).blob(self.tip, path)
                self.sources[path] = git(self.ledger.repo, "show", blob) if blob else ""
            except style_ledger.GitReadFailed:
                raise
            except style_ledger.LedgerError:
                self.sources[path] = ""
        return self.sources[path]

    def located_line(self, finding: style_ledger.HeldFinding) -> int:
        lines = self.source(finding.path).splitlines()
        matches = [line for line, text in enumerate(lines, 1) if text.strip() == finding.text.strip()]
        if not matches:
            return 0
        distance = min(abs(line - finding.line) for line in matches)
        nearest = [line for line in matches if abs(line - finding.line) == distance]
        return nearest[0] if len(nearest) == 1 else 0

    def allows(self, finding: style_ledger.HeldFinding, family: rules.Family) -> bool:
        if family == "names":
            rename = binding_rename(finding.kind, finding.before, finding.after, finding.line, finding.text)
            if not isinstance(rename, BindingRename):
                return False
            if finding.path not in self.binding_scopes:
                self.binding_scopes[finding.path] = BindingScopeReader(self.source(finding.path))
            return isinstance(self.binding_scopes[finding.path].scope(rename), BindingScope)
        if family != "values" or PurePosixPath(finding.rule.partition(":")[2]).stem != "no-magic-values":
            return False
        if finding.path not in self.test_ranges:
            source = self.source(finding.path)
            tokenized = style_footprint.tokenized_text(source)
            path = PurePosixPath(finding.path)
            self.test_ranges[finding.path] = (
                ((1, len(source.splitlines())),) if "tests" in path.parts or path.name == "tests.rs" else
                tuple((tokenized.lines[start], tokenized.lines[end - 1])
                      for start, end in style_footprint.cfg_test_items(tuple(t.value for t in tokenized.tokens))))
        line = self.located_line(finding)
        modules = [(start, end) for start, end in self.test_ranges[finding.path] if start <= line <= end]
        if not modules:
            return False
        if not self.types_loaded:
            definitions = {rule.rule_id: rule for rule in rules.load_rules(self.ledger.repo)}
            for held in self.ledger.held_findings(style_ledger.AllHeldFindings()):
                definition = definitions.get(held.rule.removesuffix("#uncovered"))
                if (definition is not None and definition.family == "types"
                        and isinstance(held.state, style_ledger.Unfiled | style_ledger.Filed)):
                    self.types.setdefault(held.path, []).append(held)
            self.types_loaded = True
        # An unlocated types site cannot establish that the module is free of types work.
        occupied = [self.located_line(held) for held in self.types.get(finding.path, ())]
        return 0 not in occupied and any(not any(start <= other <= end for other in occupied) for start, end in modules)


class ProposalGitReader(Protocol):
    """The git inputs shared by refill and the split scan."""

    def blob(self, commit: str, path: str) -> str: ...

    def crate_directories(self, commit: str) -> tuple[str, ...]: ...


class GitProposalReader:
    """Read proposal inputs with refill's existing git commands."""

    def __init__(self, ledger: style_ledger.Ledger) -> None:
        self.ledger: style_ledger.Ledger = ledger

    def blob(self, commit: str, path: str) -> str:
        try:
            # A successful tree read distinguishes an absent file from a failed Git read.
            listing = git(self.ledger.repo, "ls-tree", "-z", "--full-tree", commit, "--", path)
            for entry in filter(None, listing.split("\0")):
                metadata, name = entry.split("\t", 1)
                _mode, kind, blob = metadata.split()
                if name == path and kind == "blob":
                    return blob
            return ""
        except style_ledger.GitReadFailed:
            raise
        except style_ledger.LedgerError:
            return ""

    def crate_directories(self, commit: str) -> tuple[str, ...]:
        return tuple(member.directory for member in coverage_report.workspace(self.ledger, commit))


@dataclass(frozen=True)
class DefaultProposalReads:
    """Use refill's existing git commands."""


_DEFAULT_PROPOSAL_READS = DefaultProposalReads()


def judged_at_current_file(ledger: style_ledger.Ledger, finding: style_ledger.HeldFinding, tip: str) -> bool:
    """Whether the shared declaration evidence retains this finding's source site."""
    return isinstance(ledger.finding_site(finding, tip), style_ledger.UnchangedSite)


def print_finding_waits(issue: Path, waits: Mapping[tuple[str, str], int]) -> None:
    """One waiting line per file and unchanged reason, with the affected finding count."""
    for (path, reason), count in sorted(waits.items()):
        noun = "finding" if count == 1 else "findings"
        print(f"waiting {issue}: {path} {reason} ({count} {noun})")


def ranked_work(ledger: style_ledger.Ledger, cells: Iterable[style_ledger.Cell],
                scopes: Mapping[rules.Family, PilotScope], *,
                reader: ProposalGitReader | DefaultProposalReads = _DEFAULT_PROPOSAL_READS) -> RankedWork:
    """The open work of each family that has a unit, in that unit's pilot scope.

    Open work at a file is a cell in OPEN_CELL_STATES, a filed finding (its decision unanswered or approved and not
    built), or an unfiled finding judged at the file the synced tip has. A finding takes its family from its own rule
    in the catalog, so it counts also when its rule has no cell.
    """

    def counts(family: rules.Family, crate: str) -> bool:
        return family in scopes and crate_in_scope(scopes[family], crate)

    work: dict[str, set[rules.Family]] = defaultdict(set)
    for cell in cells:
        if cell.family is not None and cell.state in OPEN_CELL_STATES and counts(cell.family, cell.crate):
            work[cell.path].add(cell.family)
    findings = tuple(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())
                     if isinstance(finding.state, style_ledger.Filed | style_ledger.Unfiled))
    if findings:
        if isinstance(reader, DefaultProposalReads):
            reader = GitProposalReader(ledger)
        families: dict[str, rules.Family | None] = {rule.rule_id: rule.family for rule in rules.load_rules(ledger.repo)}
        tip = ledger.synced_commit()
        directories = reader.crate_directories(tip)
        eligible = tuple(finding for finding in findings
                         if (family := families.get(finding.rule.removesuffix("#uncovered"))) is not None
                         and counts(family, crate_for(directories, finding.path)))
        evidence = ledger.finding_sites((finding for finding in eligible if isinstance(finding.state, style_ledger.Unfiled)), tip)
        for finding in eligible:
            family = families.get(finding.rule.removesuffix("#uncovered"))
            if family is not None and counts(family, crate_for(directories, finding.path)) and (
                    isinstance(finding.state, style_ledger.Filed)
                    or isinstance(evidence[finding.finding_id], style_ledger.UnchangedSite)):
                work[finding.path].add(family)
    return RankedWork({path: frozenset(present) for path, present in work.items()})


@dataclass(frozen=True)
class FilingResult:
    filed: tuple[Path, ...]
    skipped: tuple[Path, ...]


@dataclass(frozen=True)
class WaiveWithAnswer:
    """The user declined the decision. Its words waive each finding that joins it."""
    words: str


@dataclass(frozen=True)
class ReopenBuiltDecision:
    """The decision is closed and nothing is left to build in it: each finding is built, waived or withdrawn.

    A finding that joins it opens it again with its answer.
    """


@dataclass(frozen=True)
class JoinOpenDecision:
    """The decision is open. A finding joins it with no change to its answer."""


JoinAction = WaiveWithAnswer | ReopenBuiltDecision | JoinOpenDecision


def join_action(ledger: style_ledger.Ledger, issue: Path) -> JoinAction:
    """What the decision's answer does to findings that join it.

    Refuse a closed decision with a filed finding, or one with no answer that a person closed.
    """
    fields = frontmatter(issue)
    answer = decision_answer(Issue(issue, fields))
    if isinstance(answer, DeclinedDecision):
        return WaiveWithAnswer(answer.words)
    closed = fields.get("status") == ["closed"]
    if closed:
        if not isinstance(answer, ApprovedDecision) and not fields.get("reason", [""])[0].startswith(FINISHED_REASONS):
            raise style_ledger.LedgerError(f"{issue}: closed decision has no approval")
        if left_to_build(issue_findings(ledger, issue)):
            raise style_ledger.LedgerError(f"{issue}: closed decision has unbuilt findings")
    try:
        require_generated_body(issue)
    except Refusal as error:
        raise style_ledger.LedgerError(str(error)) from error
    return ReopenBuiltDecision() if closed else JoinOpenDecision()


def apply_answer(ledger: style_ledger.Ledger, issue: Path, action: JoinAction, finding_ids: tuple[str, ...]) -> None:
    """Waive the joining findings of a declined decision, or reopen a built one.

    The answer acts only on pending findings, so with none it does nothing.
    """
    if not finding_ids:
        return
    if isinstance(action, WaiveWithAnswer):
        _ = ledger.decline_findings(finding_ids, action.words, style_ledger.WaiveByPerson("user"))
    elif isinstance(action, ReopenBuiltDecision):
        proposal_command("reopen", str(issue))


def file_decision(writer: DecisionWriter, key: DecisionKey, items: tuple[style_ledger.HeldFinding, ...]) -> JoinAction:
    """File the findings to their decision, or waive them when it is declined. Return what its answer did to them."""
    issue = find_decision(writer.proposals, key)
    if not issue.exists():
        issue = writer.create(key, items, {})
    action = join_action(writer.ledger, issue)
    apply_answer(writer.ledger, issue, action, tuple(finding.finding_id for finding in items))
    if not isinstance(action, WaiveWithAnswer):
        for finding in items:
            writer.ledger.file_held(finding.finding_id, issue)
    return action


def file_proposals(ledger: style_ledger.Ledger, proposals: Path, parent: str) -> FilingResult:
    writer = decision_writer(ledger, proposals, parent)
    # Filing never puts a site whose declaration moved or is gone in a decision. Its left lines wait for a person.
    follow = run_follow(writer, unfiled_and_filed(ledger), dry_run=False)
    print(follow_totals(follow))
    skipped = [*follow.skipped, *run_withdrawal(writer, dry_run=False)]
    directories = workspace_directories(ledger)
    waiting = ledger.held_findings(style_ledger.UnfiledFindingsFromOpenTickets())
    for ticket, count in sorted(Counter(finding.ticket for finding in waiting).items()):
        print(f"held for open ticket {ticket}: {count} findings")
    waiting_ids = {finding.finding_id for finding in waiting}
    batches: dict[DecisionKey, list[style_ledger.HeldFinding]] = defaultdict(list)
    candidates: list[tuple[style_ledger.HeldFinding, rules.Family | None]] = []
    for finding in ledger.held_findings(style_ledger.HeldFindingsInState("unfiled")):
        if finding.finding_id in waiting_ids:
            continue
        if style_ledger.rule_changed_since_judged(finding, writer.definitions):
            print(f"skipped {finding.finding_id}: rule changed since judged")
        else:
            candidates.append((finding, writer.definitions[finding.rule.removesuffix("#uncovered")].family))
    # The units and cells are read only when a finding of a family ranked below another can wait.
    scopes: Mapping[rules.Family, PilotScope] = {}
    work = RankedWork({})
    if isinstance(writer.standing, StandingStyleProduction) and any(
            family is not None and rules.ranked_above(family) for _, family in candidates):
        scopes = family_pilot_scopes(writer.standing.doc, ledger.repo)
        work = ranked_work(ledger, ledger.cells(), scopes)
    family_waits = {family: work.waits(family) for family in rules.FAMILY_RANKS}
    tip = ledger.synced_commit()
    exemptions = RankExemptions(ledger, tip)
    waits: set[str] = set()
    for finding, family in candidates:
        if family is not None and ranked_below_unit(family, crate_for(directories, finding.path), scopes):
            wait = family_waits[family].get(finding.path)
            if wait is not None and not exemptions.allows(finding, family):
                waits.add(wait.line())
                continue
            # A lower-rank finding judged at an older file waits for the judgment that reads the file again.
            if not judged_at_current_file(ledger, finding, tip):
                print(f"waiting {finding.finding_id}: {finding.path} changed since it was judged")
                continue
        batches[decision_key(finding, directories)].append(finding)
    for line in sorted(waits):
        print(line)
    written: list[Path] = []
    for key, items in sorted(batches.items()):
        issue = proposals / batch_name(key)
        try:
            action = file_decision(writer, key, tuple(items))
            if isinstance(action, WaiveWithAnswer):
                print(f"waived {len(items)} findings in {issue}: declined: {action.words}")
            else:
                written.append(issue)
                print(f"filed {issue}")
        except (style_ledger.LedgerError, Refusal) as error:
            skipped.append(issue)
            print(skip_line(issue, error))
    # Also finish a refresh interrupted after all ledger moves reached disk.
    # Only an open decision can get new findings, so a closed one needs no repair.
    # An open decision filed before its rule was always approved leaves the user's list here, whatever its blob.
    for decision in open_issues(proposals, "style"):
        if decision.path not in skipped and is_decision(decision):
            try:
                # Only a decision whose findings this ledger holds is this production's own.
                if issue_findings(ledger, decision.path):
                    writer.approve_by_rule(decision)
                    writer.refresh(Issue(decision.path, frontmatter(decision.path)))
            except (style_ledger.LedgerError, Refusal) as error:
                skipped.append(decision.path)
                print(skip_line(decision.path, error))
    return FilingResult(tuple(written), tuple(skipped))


def skip_line(issue: Path, error: ValueError) -> str:
    return f"skipped {issue}: {str(error).removeprefix(f'{issue}: ')}"


def rank_proposals(ledger: style_ledger.Ledger, proposals: Path) -> None:
    definitions = {rule.rule_id: rule for rule in rules.load_rules(ledger.repo)}
    directories = workspace_directories(ledger)
    goals: dict[str, str] = {}
    ranked = 0
    for issue in open_issues(proposals, "style"):
        if not is_decision(issue):
            continue
        try:
            findings = issue_findings(ledger, issue.path)
            if not findings:
                raise style_ledger.LedgerError("no decision findings")
            unknown = sorted({finding.rule.removesuffix("#uncovered") for finding in findings} - set(definitions))
            if unknown:
                raise style_ledger.LedgerError(f"unknown rule {unknown[0]}")
            if len({crate_for(directories, finding.path) for finding in findings}) != 1:
                raise style_ledger.LedgerError("findings do not form one proposal batch")
            project = plain(issue.value("project"))
            if project not in goals:
                goals[project] = project_goal(proposals, project)
            ranking = proposed_ranking(decision_kind(findings, definitions), len(findings), goals[project])
            if fill_ranking(issue.path, ranking):
                ranked += 1
                print(f"ranked {issue.path}")
        except (style_ledger.LedgerError, Refusal, OSError, UnicodeError) as error:
            print(f"skipped {issue.path}: {error}")
    print(f"ranked {ranked} issues")


def files_at(repo: Path, tip: str, paths: tuple[str, ...]) -> frozenset[str]:
    """The paths that are files at the tip."""
    if not paths:
        return frozenset()
    return frozenset(path for path in git(repo, "ls-tree", "-r", "-z", "--name-only", tip, "--", *paths).split("\0") if path)


class MovedSites:
    """Where filed findings' sites went between the commit each was judged at and the synced tip, read once per refill."""

    def __init__(self, ledger: style_ledger.Ledger, tip: str) -> None:
        self.ledger: style_ledger.Ledger = ledger
        self.tip: str = tip
        self.changed: dict[str, tuple[str, ...]] = {}
        self.lines: dict[tuple[str, str], frozenset[str]] = {}

    def destinations(self, findings: tuple[style_ledger.HeldFinding, ...]) -> dict[str, str]:
        """The file at tip that each moved finding's site went to, by finding id.

        A site moved when no line of its file at tip contains the finding's one-line text, stripped, and it moved into
        exactly one file that gained the line since the finding was judged: changed since that commit, without the line
        there and with it at tip. A finding with no judged commit, or whose text is still in its file, or went to no
        file or to several, is left out.
        """
        moved: dict[str, str] = {}
        for finding in findings:
            text = finding.text.strip()
            if len(text.splitlines()) != 1 or any(text in line for line in self.lines_at(self.tip, finding.path)):
                continue
            judged = self.ledger.judged_commit(finding)
            if isinstance(judged, style_ledger.NoJudgedCommit):
                continue
            gained = [path for path in self.changed_since(judged.commit)
                      if path != finding.path and text in self.lines_at(self.tip, path)
                      and text not in self.lines_at(judged.commit, path)]
            if len(gained) == 1:
                moved[finding.finding_id] = gained[0]
        return moved

    def changed_since(self, commit: str) -> tuple[str, ...]:
        """The files at tip that changed since commit, also the ones added."""
        if commit not in self.changed:
            # `-z --name-status` prints `<status>\0<path>\0` per file; without renames a moved file reads as added.
            fields = git(self.ledger.repo, "diff", "--no-renames", "--ignore-submodules", "--name-status", "-z",
                         "--diff-filter=AMT", commit, self.tip).split("\0")
            statuses = dict(zip(fields[1::2], fields[::2]))
            # An added file has no line at commit.
            self.lines.update({(commit, path): frozenset() for path, status in statuses.items() if status == "A"})
            self.changed[commit] = tuple(statuses)
        return self.changed[commit]

    def lines_at(self, commit: str, path: str) -> frozenset[str]:
        """The stripped lines of path at commit, a file there."""
        key = (commit, path)
        if key not in self.lines:
            self.lines[key] = frozenset(line.strip() for line in file_text(self.ledger.repo, commit, path).splitlines())
        return self.lines[key]


def close_finished_decision(ledger: style_ledger.Ledger, issue: Path) -> bool:
    """Close an open, approved decision when every one of its findings is built, waived or withdrawn. True when it closes.

    The findings it reads leave out the withdrawn ones, so they count as resolved, and a decision whose findings
    are all withdrawn is left to the withdrawal, whose reason names the judgment rows. A legacy proposal issue is
    not a decision, so it stays open for migrate.
    """
    if not issue.is_file():
        return False
    decision = Issue(issue, frontmatter(issue))
    if (not is_decision(decision) or decision.value("status") != "open"
            or not isinstance(decision_answer(decision), ApprovedDecision)):
        return False
    findings = issue_findings(ledger, issue)
    if not findings or left_to_build(findings):
        return False
    tickets = sorted(ticket["id"] for ticket in ledger.proposal_tickets()
                     if ticket["state"] == "merged" and Path(ticket["proposal"]) == issue)
    if tickets:
        reason = BUILT_REASON + ", ".join(tickets)
    else:
        # Refill waives a finding whose file is gone, so nothing was built. A plain merge or the declaration step
        # builds a finding with no ticket, so the synced commit holds its change.
        commit = ledger.synced_commit()
        paths = tuple(sorted({item.path for item in findings}))
        removed = sorted(set(paths) - files_at(ledger.repo, commit, paths))
        if removed:
            reason = NOTHING_LEFT_REASON + f"{', '.join(removed)} not in synced commit {commit}"
        elif any(isinstance(item.state, style_ledger.Built) for item in findings):
            reason = BUILT_REASON + f"synced commit {commit}"
        else:
            reason = NOTHING_LEFT_REASON + "every finding is waived"
    proposal_command("close", str(issue), "--reason", reason)
    print(f"closed {issue}: {reason}")
    return True


def complete(ledger: style_ledger.Ledger) -> None:
    closed = [issue for issue in ledger.proposal_paths() if close_finished_decision(ledger, issue)]
    # Complete and refill both close decisions. A close whose renumber failed is not seen again,
    # so any merged build renumbers once more.
    if closed or any(ticket["state"] == "merged" for ticket in ledger.proposal_tickets()):
        renumber()


def decline(ledger: style_ledger.Ledger, issue: Path, words: str) -> None:
    # The answer goes first, so a stop after it never leaves an approved decision with its findings waived.
    if decision_answer(Issue(issue, frontmatter(issue))) != DeclinedDecision(words):
        proposal_command("decline-answer", str(issue), "--words", words)
    filed = tuple(finding.finding_id for finding in issue_findings(ledger, issue)
                  if isinstance(finding.state, style_ledger.Filed))
    for finding_id, outcome in ledger.decline_findings(filed, words, style_ledger.WaiveByPerson("user")).items():
        if isinstance(outcome, style_ledger.InputChangedSinceJudged):
            print(f"not re-recorded {finding_id}: input changed since judged")
    if frontmatter(issue).get("status") != ["closed"]:
        proposal_command("close", str(issue), "--reason", f"declined: {words}")
    renumber()


def unwaive(ledger: style_ledger.Ledger, proposals: Path, names: tuple[str, ...], person: style_ledger.WaiveUndoneBy) -> None:
    """Set waived findings back to unfiled. Refuse all of them when a declined decision would waive one again at the next file."""
    directories = workspace_directories(ledger)
    for finding in ledger.named_held(names):
        issue = find_decision(proposals, decision_key(finding, directories))
        if issue.exists() and isinstance(decision_answer(Issue(issue, frontmatter(issue))), DeclinedDecision):
            raise style_ledger.LedgerError(
                f"{finding.finding_id}: declined decision {issue} waives it again at the next file; answer the decline again first")
    for finding_id in ledger.unwaive_held(names, person):
        print(f"unwaived {finding_id} by {person.by}")


def sites(ledger: style_ledger.Ledger, issue: Path) -> None:
    """Print every finding filed to the issue with its state, the withdrawn ones too."""
    for finding in ledger.proposal_findings(issue):
        print(f"{finding.finding_id} {style_ledger.held_state_name(finding.state)} {finding.path}:{finding.line}\nBefore: {finding.before}\nAfter: {finding.after}")


def candidates(ledger: style_ledger.Ledger) -> None:
    groups: dict[str, list[style_ledger.RuleCandidate]] = defaultdict(list)
    for candidate in ledger.rule_candidates():
        groups[candidate.idea].append(candidate)
    for idea, items in sorted(groups.items()):
        print(idea)
        for item in items:
            print(f"  {item.path}:{item.line} — {item.ticket} ({item.unit})")


def drop_candidate(ledger: style_ledger.Ledger, idea: str) -> None:
    print(f"dropped {ledger.drop_candidates(idea)} candidates")


@dataclass(frozen=True)
class LegacyIssue:
    issue: Issue
    # Only the findings that this migration takes from the issue.
    owned: tuple[style_ledger.HeldFinding, ...]


def off_rule_issues(ledger: style_ledger.Ledger, proposals: Path) -> tuple[Path, ...]:
    """The open issues that a style unit filed by hand through `/issue`, by name. Migrate never writes them."""
    proposer = style_slug(ledger.repo)
    # A unit files as `<unit> (<style slug>)`; the filing code files as the slug alone.
    filers = frozenset((proposer, f"({proposer})"))
    named = frozenset(ledger.proposal_paths())
    # An issue that lists findings is an old style issue, and migrate takes it.
    return tuple(issue.path for issue in open_issues(proposals, "issue")
                 if issue.value("proposed_by").rsplit(" ", 1)[-1] in filers and not issue.path.name.startswith("style ")
                 and not issue.fields.get("style_findings") and issue.path not in named)


@dataclass(frozen=True)
class NewDecision:
    """The decision does not exist yet. It takes each field that all its old issues agree on."""
    inherited: Mapping[str, str]


@dataclass(frozen=True)
class DecisionMove:
    """One decision and the findings that move into it."""
    key: DecisionKey
    issue: Path
    findings: tuple[style_ledger.HeldFinding, ...]
    join: NewDecision | JoinAction
    # The findings that take the decision's answer.
    pending: tuple[str, ...]
    # The count of findings that no earlier run moved.
    move_count: int


@dataclass(frozen=True)
class OldIssueClose:
    issue: Path
    reason: str


@dataclass(frozen=True)
class ResumeMigration:
    """Migrate takes the findings filed to each old issue, and again those an earlier run that stopped moved."""


@dataclass(frozen=True)
class FileBuiltAgain:
    """Reopen takes the built findings that are not in the code, and the findings still filed to the issue."""
    # Each built finding that reopen files again, with why.
    refilings: Mapping[str, style_ledger.PersonReading]


MoveScope = ResumeMigration | FileBuiltAgain


@dataclass(frozen=True)
class MovePlan:
    """What a move of old issues' findings into their decisions writes. Making the plan writes nothing."""
    sources: tuple[LegacyIssue, ...]
    moves: tuple[DecisionMove, ...]
    closes: tuple[OldIssueClose, ...]
    # Built findings that are not in the code, with why. The move files them again before it moves them.
    refile: Mapping[str, style_ledger.PersonReading]
    # Each old issue that keeps its findings, with the reason.
    refusals: tuple[str, ...]
    # The decision of each finding that the move takes from an old issue.
    decisions: Mapping[str, Path]
    # The refused and kept lines of the old issues, in order.
    issue_lines: tuple[str, ...]
    # The bare duplicates that leave their old issue as withdrawn rows of the decision that holds their qualified finding.
    duplicates: tuple[style_ledger.BareDuplicate, ...] = ()


def plan_moves(ledger: style_ledger.Ledger, proposals: Path, issues: list[Issue], scope: MoveScope) -> MovePlan:
    """Plan the move of the findings scope takes from each old issue into their decisions, and each open issue's close."""
    all_findings = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
    open_tickets = tuple(ticket for ticket in ledger.proposal_tickets()
                         if ticket["state"] not in style_ledger.FINAL_TICKET_STATES)
    holding = {Path(ticket["proposal"]): ticket for ticket in open_tickets}
    open_ids = {ticket["id"] for ticket in open_tickets}
    refile: Mapping[str, style_ledger.PersonReading] = scope.refilings if isinstance(scope, FileBuiltAgain) else {}
    directories = workspace_directories(ledger)
    lines: list[str] = []
    refusals: list[str] = []
    sources: list[LegacyIssue] = []
    keys: dict[str, DecisionKey] = {}
    origin: dict[str, Issue] = {}
    moved_before: set[str] = set()
    for issue in issues:
        identities = tuple(dict.fromkeys(issue.fields.get("style_findings", [])))
        if not identities:
            continue
        # An earlier run that stopped moved these findings from this issue. Reopen moves only what it files again.
        moved = ledger.moved_findings(issue.path) if isinstance(scope, ResumeMigration) else {}
        taken = frozenset(identity for identity in identities if identity in moved or identity in refile or (
            identity in all_findings and isinstance(state := all_findings[identity].state, style_ledger.Filed)
            and state.proposal == issue.path))
        # Migrate leaves an old issue whole while any of its findings is being built; reopen looks only at what it takes.
        checked = identities if isinstance(scope, ResumeMigration) else tuple(sorted(taken))
        if issue.path in holding:
            ticket = holding[issue.path]
            refusals.append(f"{issue.path}: live build ticket {ticket['id']} ({ticket['state']})")
        elif any(identity not in all_findings for identity in identities):
            refusals.append(f"{issue.path}: unknown findings")
        elif any(isinstance(build := all_findings[identity].build_ticket, style_ledger.BuildTicket)
                 and build.ticket in open_ids for identity in checked):
            refusals.append(f"{issue.path}: live build ticket")
        else:
            owned: list[style_ledger.HeldFinding] = []
            for finding in (all_findings[identity] for identity in identities):
                if finding.finding_id in taken:
                    owned.append(finding)
                    keys[finding.finding_id] = decision_key(finding, directories)
                    origin[finding.finding_id] = issue
                else:
                    lines.append(f"kept {finding.finding_id}: {style_ledger.held_state_name(finding.state)}, not filed to {issue.path}")
            moved_before.update(moved)
            sources.append(LegacyIssue(issue, tuple(owned)))
            continue
        lines.append(f"refused {refusals[-1]}")
    groups: dict[DecisionKey, list[style_ledger.HeldFinding]] = defaultdict(list)
    for source in sources:
        for finding in source.owned:
            groups[keys[finding.finding_id]].append(finding)
    moves: list[DecisionMove] = []
    duplicates: list[style_ledger.BareDuplicate] = []
    for key, grouped in sorted(groups.items()):
        issue = find_decision(proposals, key)
        # A bare duplicate never moves into the decision that holds, or receives now, its qualified finding.
        held = issue_findings(ledger, issue) if issue.exists() else ()
        dropped = bare_duplicates(tuple(grouped), held + tuple(grouped))
        duplicates.extend(style_ledger.BareDuplicate(finding_id, qualified, issue) for finding_id, qualified in dropped.items())
        findings = [finding for finding in grouped if finding.finding_id not in dropped]
        join: NewDecision | JoinAction
        if issue.exists():
            # The answer is read also when every finding is a duplicate, so its refusal comes before any write.
            join = join_action(ledger, issue)
            if not findings:
                continue
        else:
            origins = sorted({origin[finding.finding_id].path: origin[finding.finding_id] for finding in findings}.values(),
                             key=lambda item: item.path)
            join = NewDecision({name: origins[0].value(name) for name in sorted(INHERITED_FIELDS)
                                if origins[0].value(name) and all(item.value(name) == origins[0].value(name) for item in origins)})
        # A finding still filed has not yet taken the decision's answer.
        pending = tuple(finding.finding_id for finding in findings
                        if isinstance(finding.state, style_ledger.Filed) or finding.finding_id in refile)
        moves.append(DecisionMove(key, issue, tuple(findings), join, pending,
                                  sum(finding.finding_id not in moved_before for finding in findings)))
    decisions = {finding.finding_id: move.issue for move in moves for finding in move.findings}
    destinations = decisions | {duplicate.finding_id: duplicate.decision for duplicate in duplicates}
    closes: list[OldIssueClose] = []
    for source in sources:
        # A closed old issue that reopen repairs keeps its close reason.
        if source.issue.value("status") != "open":
            continue
        names = sorted({destinations[finding.finding_id].name for finding in source.owned})
        closes.append(OldIssueClose(source.issue.path, "merged into " + ", ".join(names) if names else "no filed findings"))
    dropped_ids = {duplicate.finding_id for duplicate in duplicates}
    return MovePlan(tuple(sources), tuple(moves), tuple(closes),
                    {finding_id: reading for finding_id, reading in refile.items() if finding_id not in dropped_ids},
                    tuple(refusals), decisions, tuple(lines), tuple(sorted(duplicates, key=lambda item: item.finding_id)))


def run_moves(ledger: style_ledger.Ledger, proposals: Path, parent: str, plan: MovePlan,
              say: Callable[[str], None], *, dry_run: bool) -> None:
    """Say each line of a planned move just before its write, and write it unless dry run.

    A built decision opens before any finding moves into it, so a stop never leaves a pending finding in a closed decision.
    """
    writer = decision_writer(ledger, proposals, parent)
    # A decision that this run creates names the run's parent. A refresh keeps the parent that any other decision
    # names, read here before the first write.
    created = {move.issue for move in plan.moves if isinstance(move.join, NewDecision)}
    refreshers = {issue: writer if issue in created else replace(writer, parent=decision_parent(issue))
                  for issue in sorted(set(plan.decisions.values()))}
    for line in plan.issue_lines:
        say(line)
    for move in plan.moves:
        if isinstance(move.join, NewDecision):
            say(f"create {move.issue}")
            if not dry_run:
                _ = writer.create(move.key, move.findings, move.join.inherited)
        say(f"move {move.move_count} findings to {move.issue}")
        if isinstance(move.join, WaiveWithAnswer):
            say(f"waive {len(move.pending)} findings in {move.issue}: declined: {move.join.words}")
        elif isinstance(move.join, ReopenBuiltDecision) and move.pending:
            say(f"reopen {move.issue}")
            if not dry_run:
                apply_answer(ledger, move.issue, move.join, move.pending)
    for duplicate in plan.duplicates:
        say(duplicate_line(duplicate))
    if not dry_run:
        if plan.refile:
            ledger.return_to_filed(plan.refile)
        for source in plan.sources:
            moving = {finding.finding_id: plan.decisions[finding.finding_id] for finding in source.owned
                      if finding.finding_id in plan.decisions}
            if moving:
                ledger.move_issue_findings(source.issue.path, moving)
        if plan.duplicates:
            ledger.withdraw_duplicates(plan.duplicates)
        for move in plan.moves:
            if isinstance(move.join, WaiveWithAnswer):
                apply_answer(ledger, move.issue, move.join, move.pending)
        for issue, refresher in refreshers.items():
            refresher.refresh(Issue(issue, frontmatter(issue)))
    for close in plan.closes:
        say(f"close {close.issue}: {close.reason}")
        if not dry_run:
            proposal_command("close", str(close.issue), "--reason", close.reason)


def migrate(ledger: style_ledger.Ledger, proposals: Path, parent: str, *, dry_run: bool = False) -> None:
    plan = plan_moves(ledger, proposals, open_issues(proposals, "style"), ResumeMigration())
    run_moves(ledger, proposals, parent, plan, print, dry_run=dry_run)
    off_rule = off_rule_issues(ledger, proposals)
    for issue in off_rule:
        print(f"skipped: off-rule {issue}")
    created = sum(isinstance(move.join, NewDecision) for move in plan.moves)
    moved = sum(move.move_count for move in plan.moves)
    print(f"totals: create {created}, move {moved}, close {len(plan.closes)}, off-rule {len(off_rule)}")


@dataclass(frozen=True)
class CheckOnly:
    """Reopen files again only the built findings that the applied check finds not in the code."""


@dataclass(frozen=True)
class PersonRead:
    """A person read the code of the named built findings in place of the applied check.

    Reopen files each not_applied finding again and keeps each applied finding built. Each name is a finding id
    or a unique prefix of one.
    """
    by: str
    not_applied: tuple[str, ...]
    applied: tuple[str, ...]


ReopenReading = CheckOnly | PersonRead
CHECK_ONLY = CheckOnly()


@dataclass(frozen=True)
class PersonVerdicts:
    """The built findings of one issue that a person read, by verdict, each with the person's reading."""
    not_applied: Mapping[str, style_ledger.ReadByPerson]
    applied: Mapping[str, style_ledger.ReadByPerson]


@dataclass(frozen=True)
class Reopening:
    """What reopen does to one issue."""
    filed_again: tuple[style_ledger.HeldFinding, ...]
    # The built findings that a person found applied in this run.
    applied: tuple[style_ledger.HeldFinding, ...]
    # The built renames that the applied check cannot judge and no person read. A person reads each one.
    unchecked_renames: tuple[style_ledger.HeldFinding, ...]
    # The built findings that are not renames, which the applied check never judges.
    unjudged_restructures: tuple[style_ledger.HeldFinding, ...]


@dataclass(frozen=True)
class ReopenSweep:
    """What reopen --all leaves to a person: the issues it skipped and the count of built findings it cannot check."""
    skipped: tuple[Path, ...]
    unchecked_renames: int


def reopen(ledger: style_ledger.Ledger, issue: Path, parent: str, *, reading: ReopenReading = CHECK_ONLY,
           dry_run: bool = False) -> tuple[style_ledger.HeldFinding, ...]:
    """Set each built rename of the issue that is not in the synced code back to filed, in an approved decision.

    Every refusal comes before the first write. Returns the built renames that the applied check cannot judge.
    """
    result = file_unapplied_again(ledger, issue, parent, reading, dry_run=dry_run)
    if not result.filed_again and not result.applied and not result.unchecked_renames:
        # The check never judges a restructure, so it cannot say that one is applied.
        print(f"nothing to reopen: every checked finding is applied; restructure not checked {len(result.unjudged_restructures)}"
              if result.unjudged_restructures else "nothing to reopen: every built finding is applied")
    elif result.filed_again and not dry_run:
        renumber()
    return result.unchecked_renames


def old_issue_parent(issue: Path, given: str) -> str:
    """The parent that the old issue's Parent line names, else the given parent."""
    match = PARENT_LINE.search(issue.read_text())
    if match is not None:
        return match[1]
    if not given:
        raise style_ledger.LedgerError("no Parent line; pass --parent")
    return given


def reopen_all(ledger: style_ledger.Ledger, given_parent: str, *, dry_run: bool = False) -> ReopenSweep:
    """Reopen each approved decision and old issue that holds a built finding, then print the totals.

    An old issue takes the parent its Parent line names, else given_parent. Each issue that the sweep
    leaves out or reopen refuses prints a skip line and the others go on.
    """
    built = {finding.state.proposal for finding in ledger.held_findings(style_ledger.HeldFindingsInState("built"))
             if isinstance(finding.state, style_ledger.Built)}
    reopened = 0
    filed_again = 0
    unchecked_renames = 0
    unjudged_restructures = 0
    skipped: list[Path] = []
    for issue in sorted(built):
        try:
            if not issue.is_file():
                raise style_ledger.LedgerError("missing file")
            source = Issue(issue, frontmatter(issue))
            answer = decision_answer(source)
            if not isinstance(answer, ApprovedDecision):
                raise style_ledger.LedgerError("closed without answer" if isinstance(answer, UnansweredDecision)
                                               and source.value("status") == "closed" else "not approved")
            # A decision moves no finding, so it needs no parent.
            parent = "" if is_decision(source) else old_issue_parent(issue, given_parent)
            result = file_unapplied_again(ledger, issue, parent, CHECK_ONLY, dry_run=dry_run)
        except style_ledger.LedgerError as error:
            skipped.append(issue)
            print(skip_line(issue, error))
            continue
        reopened += bool(result.filed_again)
        filed_again += len(result.filed_again)
        unchecked_renames += len(result.unchecked_renames)
        unjudged_restructures += len(result.unjudged_restructures)
    if reopened and not dry_run:
        renumber()
    print(f"totals: reopen {reopened}, filed again {filed_again}, cannot check {unchecked_renames}, "
          + f"restructure not checked {unjudged_restructures}, skipped {len(skipped)}")
    return ReopenSweep(tuple(skipped), unchecked_renames)


def named_built_finding(issue: Path, findings: tuple[style_ledger.HeldFinding, ...], option: str,
                        name: str) -> style_ledger.HeldFinding:
    """The one built finding of the issue that name names; refuses a name that is not one."""
    matches = [finding for finding in findings if finding.finding_id.startswith(name)]
    if not matches:
        raise style_ledger.LedgerError(f"{issue}: no finding {name} in this issue")
    if len(matches) > 1:
        raise style_ledger.LedgerError(f"{issue}: {option} {name} matches {len(matches)} findings")
    finding, = matches
    if not isinstance(finding.state, style_ledger.Built):
        raise style_ledger.LedgerError(
            f"{issue}: finding {finding.finding_id} is {style_ledger.held_state_name(finding.state)}, not built")
    return finding


def person_verdicts(issue: Path, findings: tuple[style_ledger.HeldFinding, ...], reading: ReopenReading) -> PersonVerdicts:
    """The built findings of the issue that the person read, by verdict; refuses a name that is not one, or both."""
    if isinstance(reading, CheckOnly):
        return PersonVerdicts({}, {})
    reader = style_ledger.ReadByPerson(reading.by)
    not_applied = {named_built_finding(issue, findings, "--finding", name).finding_id: reader for name in reading.not_applied}
    applied = {named_built_finding(issue, findings, "--applied", name).finding_id: reader for name in reading.applied}
    both = sorted(not_applied.keys() & applied.keys())
    if both:
        raise style_ledger.LedgerError(f"{issue}: finding {both[0]} is both --finding and --applied")
    return PersonVerdicts(not_applied, applied)


def file_unapplied_again(ledger: style_ledger.Ledger, issue: Path, parent: str, reading: ReopenReading, *,
                         dry_run: bool) -> Reopening:
    """File each built rename of the issue that is not in the synced code again, and each one a person found not applied.

    Record each built finding that a person found applied. Print a line for each finding filed again, for each
    finding a person found applied and for each built rename that the applied check cannot judge.
    Every refusal comes before the first write.
    """
    building = [ticket["id"] for ticket in ledger.proposal_tickets()
                if Path(ticket["proposal"]) == issue and ticket["state"] not in style_ledger.FINAL_TICKET_STATES]
    if building:
        raise style_ledger.LedgerError(f"{issue}: open ticket {building[0]} builds it")
    commit = ledger.synced_commit()
    # Until a sync takes in a merged ticket, the synced code lacks its renames.
    unsynced = ledger.unsynced_merges(issue)
    if unsynced:
        raise style_ledger.LedgerError(f"{issue}: merged ticket {unsynced[0]['id']} is not in synced commit "
                                       + f"{commit[:7]}; sync first")
    source = Issue(issue, frontmatter(issue))
    if not isinstance(decision_answer(source), ApprovedDecision):
        raise style_ledger.LedgerError(f"{issue}: not approved")
    decision = is_decision(source)
    findings = issue_findings(ledger, issue)
    verdicts = person_verdicts(issue, findings, reading)
    unbuilt: list[style_ledger.HeldFinding] = []
    applied: list[style_ledger.HeldFinding] = []
    unchecked_renames: list[style_ledger.HeldFinding] = []
    unjudged_restructures: list[style_ledger.HeldFinding] = []
    for finding in findings:
        # A person's verdict comes before the check, so the check does not run for it.
        if finding.finding_id in verdicts.not_applied:
            unbuilt.append(finding)
            continue
        if finding.finding_id in verdicts.applied:
            # The check is certain when the old name is still declared, so it refuses a verdict of applied.
            if isinstance(ledger.applied_check(finding, commit), RenameNotApplied):
                raise style_ledger.LedgerError(
                    f"{issue}: finding {finding.finding_id}: the check finds the rename not applied; use --finding")
            applied.append(finding)
            continue
        # A run that stopped before the move leaves an old issue's findings filed to it.
        if not (isinstance(finding.state, style_ledger.Built)
                or not decision and isinstance(finding.state, style_ledger.Filed)):
            continue
        # A person who read the code of a built finding settled it, so the check does not judge it again.
        if isinstance(finding.state, style_ledger.Built) and isinstance(finding.reading, style_ledger.ReadByPerson):
            continue
        check = ledger.applied_check(finding, commit)
        if isinstance(check, RenameNotApplied):
            unbuilt.append(finding)
        elif isinstance(check, NoAppliedCheck) and isinstance(finding.state, style_ledger.Built):
            (unchecked_renames if finding.kind == "rename" else unjudged_restructures).append(finding)
    filed_again = file_again(ledger, source, parent, tuple(unbuilt), verdicts.not_applied, dry_run=dry_run) if unbuilt else ()
    if applied and not dry_run:
        ledger.record_person_applied(verdicts.applied)
    for finding in applied:
        print(f"applied {finding.finding_id} in {issue}: {finding.before} -> {finding.after} in {finding.path} "
              + f"by {verdicts.applied[finding.finding_id].by}: a person read the code")
    for finding in unchecked_renames:
        print(f"cannot check {finding.finding_id} in {finding.path}: {finding.before} -> {finding.after}; "
              + f"pass --finding {finding.finding_id} or --applied {finding.finding_id}")
    return Reopening(filed_again, tuple(applied), tuple(unchecked_renames), tuple(unjudged_restructures))


def file_again(ledger: style_ledger.Ledger, source: Issue, parent: str, unbuilt: tuple[style_ledger.HeldFinding, ...],
               readings: Mapping[str, style_ledger.ReadByPerson], *, dry_run: bool) -> tuple[style_ledger.HeldFinding, ...]:
    """File the findings again in an approved decision and print a line for each; refusals come before the first write.

    A bare duplicate of a qualified finding in its decision is dropped, not filed again. Returns the findings filed again.
    """
    issue = source.path
    decision = is_decision(source)
    built: dict[str, style_ledger.PersonReading] = {
        finding.finding_id: readings.get(finding.finding_id, style_ledger.NotReadByPerson())
        for finding in unbuilt if isinstance(finding.state, style_ledger.Built)}
    dropped: tuple[style_ledger.BareDuplicate, ...] = ()
    if decision:
        decisions = {finding.finding_id: issue for finding in unbuilt}
        if not dry_run:
            # Open the decision first: a stop before the findings are filed again leaves it built, so complete closes it.
            if source.value("status") != "open":
                proposal_command("reopen", str(issue))
            ledger.return_to_filed(built)
    else:
        listed = set(source.fields.get("style_findings", []))
        missing = sorted(finding.finding_id for finding in unbuilt if finding.finding_id not in listed)
        if missing:
            raise style_ledger.LedgerError(f"{issue}: style_findings does not list {', '.join(missing)}")
        plan = plan_moves(ledger, issue.parent, [source], FileBuiltAgain(built))
        if plan.refusals:
            raise style_ledger.LedgerError("; ".join(plan.refusals))
        # Every move of reopen carries pending findings, so each decision must take them with an approval.
        for move in plan.moves:
            if isinstance(move.join, NewDecision):
                continue
            answer = decision_answer(Issue(move.issue, frontmatter(move.issue)))
            if isinstance(answer, DeclinedDecision):
                raise style_ledger.LedgerError(f"{issue}: decision {move.issue} is declined")
            if isinstance(answer, UnansweredDecision):
                raise style_ledger.LedgerError(f"{issue}: decision {move.issue} has no answer")
        decisions = plan.decisions
        dropped = plan.duplicates
        # Reopen prints only its own lines; the notices of a new decision go to stderr.
        with redirect_stdout(sys.stderr):
            run_moves(ledger, issue.parent, parent, plan, lambda _: None, dry_run=dry_run)
    for duplicate in dropped:
        print(duplicate_line(duplicate))
    filed = tuple(finding for finding in unbuilt if finding.finding_id in decisions)
    for finding in filed:
        cause = (f"in {finding.path} by {readings[finding.finding_id].by}: a person read the code"
                 if finding.finding_id in readings else f"not applied in {finding.path}")
        print(f"filed again {finding.finding_id} in {decisions[finding.finding_id]}: "
              + f"{finding.before} -> {finding.after} {cause}")
    return filed


PARENT_LINE = re.compile(r"^Parent: (.+)$", re.MULTILINE)


def decision_parent(issue: Path) -> str:
    """The parent that the decision's generated body names."""
    match = PARENT_LINE.search(issue.read_text())
    if match is None:
        raise style_ledger.LedgerError(f"{issue}: no Parent line")
    return match[1]


@dataclass(frozen=True)
class EmptiedDecisionClose:
    """An open decision that withdrawal leaves with no filed finding, and the reason it closes."""
    issue: Path
    reason: str


@dataclass(frozen=True)
class WithdrawalPlan:
    """What a withdrawal writes to the decisions. Making the plan writes nothing."""
    closes: tuple[EmptiedDecisionClose, ...]
    # The open decisions that keep a finding that is not withdrawn. Each refreshes its findings list.
    refreshes: tuple[Path, ...]


def plan_withdrawal(ledger: style_ledger.Ledger, pending: Mapping[str, tuple[Path, style_ledger.WithdrawnBy]]) -> WithdrawalPlan:
    """Plan the close or refresh of each open issue that holds a withdrawn finding, as if the pending rows were written.

    pending maps each filed finding to withdraw to its decision and the reason. An issue closes only when every one
    of its findings is withdrawn; one that keeps another finding is refreshed, and complete closes it when the rest
    are resolved. Findings that an earlier run withdrew count too, so a run that stopped before its closes ends on a
    rerun.
    """
    issues = {issue for issue, _ in pending.values()}
    issues.update(finding.state.proposal for finding in ledger.held_findings(style_ledger.HeldFindingsInState("withdrawn"))
                  if isinstance(finding.state, style_ledger.Withdrawn) and isinstance(finding.state.proposal, Path)
                  and isinstance(finding.state.by, (style_ledger.ExceptionVerdict, style_ledger.DeclarationGone)))
    closes: list[EmptiedDecisionClose] = []
    refreshes: list[Path] = []
    for issue in sorted(issues):
        if not issue.is_file():
            continue
        fields = frontmatter(issue)
        if fields.get("status") != ["open"]:
            continue
        findings = ledger.proposal_findings(issue)
        if any(not isinstance(finding.state, style_ledger.Withdrawn) and finding.finding_id not in pending for finding in findings):
            if is_decision(Issue(issue, fields)):
                refreshes.append(issue)
            continue
        rows: dict[str, style_ledger.WithdrawnBy] = {finding.finding_id: finding.state.by for finding in findings
                                                     if isinstance(finding.state, style_ledger.Withdrawn)}
        rows.update((finding.finding_id, pending[finding.finding_id][1]) for finding in findings if finding.finding_id in pending)
        withdrawn = ", ".join(f"{finding_id} ({style_ledger.withdrawn_line(row)})" for finding_id, row in sorted(rows.items()))
        closes.append(EmptiedDecisionClose(issue, f"{NOTHING_LEFT_REASON}withdrawn: {withdrawn}"))
    return WithdrawalPlan(tuple(closes), tuple(refreshes))


def drop_duplicates(ledger: style_ledger.Ledger, *, dry_run: bool) -> tuple[Path, ...]:
    """Drop each bare duplicate of every open decision, then refresh the decision; print a line for each and the totals.

    A decision whose count still includes a row that an earlier run dropped also refreshes, so a run that stopped
    before its refresh ends on a rerun. A decision with a duplicate that an open ticket holds, or with no generated
    body, prints a skip line and is not written. Every refusal and each decision's parent come before the first
    write. Returns the skipped decisions.
    """
    open_ids = {ticket["id"] for ticket in ledger.proposal_tickets() if ticket["state"] not in style_ledger.FINAL_TICKET_STATES}
    planned: list[tuple[Issue, DecisionWriter, tuple[style_ledger.BareDuplicate, ...]]] = []
    writers: dict[Path, DecisionWriter] = {}
    skipped: list[Path] = []
    for path in ledger.proposal_paths():
        if not path.is_file():
            continue
        decision = Issue(path, frontmatter(path))
        if not is_decision(decision) or decision.value("status") != "open":
            continue
        findings = issue_findings(ledger, path)
        by_id = {finding.finding_id: finding for finding in findings}
        dropped = bare_duplicates(findings, findings)
        stale = decision.value("style_count") != str(len(findings)) and any(
            isinstance(finding.state, style_ledger.Withdrawn) and isinstance(finding.state.by, style_ledger.DuplicateOf)
            for finding in ledger.proposal_findings(path))
        if not dropped and not stale:
            continue
        try:
            for finding_id in sorted(dropped):
                build = by_id[finding_id].build_ticket
                if isinstance(build, style_ledger.BuildTicket) and build.ticket in open_ids:
                    raise style_ledger.LedgerError(f"open ticket {build.ticket} holds {finding_id}")
            require_generated_body(path)
            parent = decision_parent(path)
        except (style_ledger.LedgerError, Refusal) as error:
            skipped.append(path)
            print(skip_line(path, error))
            continue
        if path.parent not in writers:
            writers[path.parent] = decision_writer(ledger, path.parent, "")
        planned.append((decision, replace(writers[path.parent], parent=parent),
                        tuple(style_ledger.BareDuplicate(finding_id, qualified, path) for finding_id, qualified in sorted(dropped.items()))))
    for decision, writer, duplicates in planned:
        for duplicate in duplicates:
            print(duplicate_line(duplicate))
        if not dry_run:
            if duplicates:
                ledger.withdraw_duplicates(duplicates)
            # The command prints only its own lines, as a dry run does; the notices of the refresh go to stderr.
            with redirect_stdout(sys.stderr):
                writer.refresh(Issue(decision.path, frontmatter(decision.path)))
    print(f"totals: drop {sum(len(duplicates) for _, _, duplicates in planned)}, skipped {len(skipped)}")
    return tuple(skipped)


def superseded_line(finding: style_ledger.HeldFinding, verdict: style_ledger.SupersedingJudgment) -> str:
    return (f"{finding.finding_id} {finding.rule} {finding.path}:{finding.line}: "
            + f"judgment {verdict.judgment_id} {verdict.verdict} under {verdict.rule_blob[:8]}")


def run_withdrawal(writer: DecisionWriter, *, dry_run: bool) -> tuple[Path, ...]:
    """Withdraw each filed finding that an exception under its rule's newer text supersedes, and each unfiled
    finding whose cell that text judged again, then close or refresh the decisions of the filed ones.

    Print one line per withdrawn and kept finding and per closed decision, then the totals. A dry run prints the
    same lines and writes nothing. A refresh keeps the parent that the decision names. A decision that cannot
    close or refresh prints a skip line and the others go on; returns those decisions.
    """
    ledger = writer.ledger
    withdrawal = (ledger.superseded_findings(writer.definitions) if dry_run
                  else ledger.withdraw_superseded(writer.definitions))
    plan = plan_withdrawal(ledger, {item.finding.finding_id: (item.finding.state.proposal, style_ledger.ExceptionVerdict(item.verdict.judgment_id))
                                    for item in withdrawal.withdrawn if isinstance(item.finding.state, style_ledger.Filed)})
    for item in withdrawal.withdrawn:
        print(f"withdraw {superseded_line(item.finding, item.verdict)}")
    for kept in withdrawal.kept:
        print(f"kept {superseded_line(kept.finding, kept.verdict)}; open ticket {kept.ticket} builds it")
    skipped: list[Path] = []
    closed = False
    for close in plan.closes:
        print(f"close {close.issue}: {close.reason}")
        if not dry_run:
            try:
                proposal_command("close", str(close.issue), "--reason", close.reason)
                closed = True
            except (style_ledger.LedgerError, Refusal) as error:
                skipped.append(close.issue)
                print(skip_line(close.issue, error))
    if not dry_run:
        for issue in plan.refreshes:
            try:
                replace(writer, parent=decision_parent(issue)).refresh(Issue(issue, frontmatter(issue)))
            except (style_ledger.LedgerError, Refusal) as error:
                skipped.append(issue)
                print(skip_line(issue, error))
    if closed:
        renumber()
    print(f"totals: withdraw {len(withdrawal.withdrawn)}, kept {len(withdrawal.kept)}, close {len(plan.closes)}")
    return tuple(skipped)


@dataclass(frozen=True)
class FollowRun:
    """What one declaration step read and wrote, the decisions it closed and the decisions it skipped."""
    reading: style_ledger.DeclarationReading
    closes: tuple[Path, ...]
    skipped: tuple[Path, ...]

    def count(self, kinds: type[style_ledger.DeclarationOutcome] | UnionType) -> int:
        return sum(isinstance(outcome, kinds) for outcome in self.reading.outcomes)

    def sites(self, kind: type[style_ledger.SiteEvidence]) -> int:
        return sum(isinstance(outcome, style_ledger.FindingSite) and isinstance(outcome.evidence, kind)
                   for outcome in self.reading.outcomes)


def follow_totals(run: FollowRun) -> str:
    return (f"totals: follow {run.count(style_ledger.DeclarationMoved) + run.sites(style_ledger.UnchangedSite)}, "
            + f"built {run.count(style_ledger.DeclarationRenamed)}, "
            + f"withdraw {run.count(style_ledger.DeclarationRemoved)}, "
            + f"left {run.count(style_ledger.DeclarationLeft) + run.sites(style_ledger.NeedsJudgment)}, "
            + f"kept {run.count(style_ledger.KeptForOpenBuild)}, close {len(run.closes)}")


# The outcomes that print a line: each one changes the finding, or names why the step leaves it.
DeclarationChange = (style_ledger.DeclarationMoved | style_ledger.DeclarationRemoved | style_ledger.DeclaredInSeveralFiles
                     | style_ledger.DeclaredInAnotherCrate | style_ledger.DeclarationRenamed | style_ledger.DeclarationGoneFromFile
                     | style_ledger.KeptForOpenBuild | style_ledger.TraitIdentityUncertain | style_ledger.OutsideRenameUnresolved
                     | style_ledger.StatementNotFollowed | style_ledger.FindingSite)


def follow_line(outcome: DeclarationChange, tip: str) -> str:
    """The line that names what the step does to the finding."""
    finding = outcome.finding
    site = f"{finding.finding_id} {finding.rule} {finding.path}:{finding.line}"
    if isinstance(outcome, style_ledger.FindingSite):
        match outcome.evidence:
            case style_ledger.UnchangedSite(path, line):
                return f"follow {site} -> {path}:{line}: unchanged source site"
            case style_ledger.NeedsJudgment(reason):
                return f"left {site}: needs judgment: {reason}"
    suffix = (style_ledger.outside_evidence_suffix(outcome.evidence)
              if isinstance(outcome, (style_ledger.DeclarationRemoved, style_ledger.DeclarationGoneFromFile,
                                      style_ledger.DeclaredInSeveralFiles, style_ledger.DeclaredInAnotherCrate))
              and isinstance(outcome.evidence, style_ledger.OutsideRenameNotProven) else "")
    match outcome:
        case style_ledger.OutsideRenameUnresolved():
            return f"left {site}: {style_ledger.outside_evidence_line(outcome.evidence)}"
        case style_ledger.DeclarationMoved():
            return f"follow {site} -> {outcome.path}:{outcome.line}: {declaration_line(outcome.declaration)}"
        case style_ledger.DeclarationRemoved():
            return (f"withdraw {site}: {declaration_line(outcome.declaration)} "
                    + style_ledger.withdrawn_line(style_ledger.DeclarationGone(tip)) + suffix)
        case style_ledger.DeclaredInSeveralFiles():
            return f"left {site}: {declaration_line(outcome.declaration)} declared in {', '.join(outcome.paths)}" + suffix
        case style_ledger.TraitIdentityUncertain():
            return f"left {site}: {declaration_line(outcome.declaration)} trait identity uncertain"
        case style_ledger.StatementNotFollowed():
            return f"left {site}: {declaration_line(outcome.declaration)} moved to {outcome.path}; {outcome.reason}"
        case style_ledger.DeclaredInAnotherCrate():
            return f"left {site}: {declaration_line(outcome.declaration)} declared in another crate: {outcome.path}" + suffix
        case style_ledger.DeclarationRenamed():
            return (f"built {site}: {declaration_line(outcome.declaration)} renamed to {outcome.renamed.name} "
                    + f"in {outcome.path}:{outcome.line}")
        case style_ledger.DeclarationGoneFromFile():
            return f"left {site}: {declaration_line(outcome.declaration)} declaration gone; waive it or declare it again" + suffix
        case style_ledger.KeptForOpenBuild():
            return f"kept {site}: open ticket {outcome.ticket} builds it"


def approved_decision(issue: Path) -> bool:
    """Whether issue is a decision file that the user approved."""
    return issue.is_file() and isinstance(decision_answer(Issue(issue, frontmatter(issue))), ApprovedDecision)


def open_decision(issue: Path) -> bool:
    if not issue.is_file():
        return False
    decision = Issue(issue, frontmatter(issue))
    return is_decision(decision) and decision.value("status") == "open"


def unfiled_and_filed(ledger: style_ledger.Ledger) -> tuple[style_ledger.HeldFinding, ...]:
    return (*ledger.held_findings(style_ledger.HeldFindingsInState("unfiled")),
            *ledger.held_findings(style_ledger.HeldFindingsInState("filed")))


def run_follow(writer: DecisionWriter, findings: Iterable[style_ledger.HeldFinding], *, dry_run: bool) -> FollowRun:
    """Move each finding whose declaration moved to one other file there, build each renamed finding of an approved
    decision, and withdraw each whose declaration is gone. Then close each open decision that has no finding left
    and refresh each other one that holds such a finding.

    Print one line per followed, built, withdrawn, left and kept finding and per closed decision; the caller prints
    the totals. Before the first write, a decision with no generated body or no Parent line prints a skip line, and
    the step writes none of its findings; the others go on. A renamed finding whose decision is not approved stays
    filed with no line. A refresh keeps the parent that the decision names. A dry run prints the same lines and
    writes nothing. The step refuses when the synced tip moves after its reading.
    """
    ledger = writer.ledger
    ids = tuple(finding.finding_id for finding in findings)
    planned = ledger.declaration_reading(ids, approved_decision)
    skipped: list[Path] = []
    for issue in sorted({outcome.finding.state.proposal for outcome in planned.outcomes
                         if (isinstance(outcome, (style_ledger.DeclarationMoved, style_ledger.DeclarationRemoved))
                             or isinstance(outcome, style_ledger.FindingSite) and not isinstance(outcome.evidence, style_ledger.NeedsJudgment))
                         and isinstance(outcome.finding.state, style_ledger.Filed)}):
        if not open_decision(issue):
            continue
        try:
            require_generated_body(issue)
            _ = decision_parent(issue)
        except (style_ledger.LedgerError, Refusal) as error:
            skipped.append(issue)
            print(skip_line(issue, error))
            # The source proof cannot refresh a decision without its generated body. Keep the
            # changed-file wait visible for these findings, rather than silently losing that work.
            waiting: Counter[tuple[str, str]] = Counter()
            for outcome in planned.outcomes:
                if (isinstance(outcome, style_ledger.FindingSite)
                        and isinstance(outcome.finding.state, style_ledger.Filed)
                        and outcome.finding.state.proposal == issue):
                    finding = outcome.finding
                    waiting[(finding.path, "changed since it was judged; source follow could not refresh the decision")] += 1
            print_finding_waits(issue, waiting)
    refused = {outcome.finding.finding_id for outcome in planned.outcomes
               if isinstance(outcome.finding.state, style_ledger.Filed) and outcome.finding.state.proposal in skipped}
    # Built means approved and in the code, so the ledger builds a rename only when its decision is approved, reading
    # the answer again under its write lock. The first step after the approval builds it.
    reading = (style_ledger.DeclarationReading(planned.tip, tuple(outcome for outcome in planned.outcomes
                                                                  if outcome.finding.finding_id not in refused))
               if dry_run else ledger.follow_declarations((finding_id for finding_id in ids if finding_id not in refused),
                                                          planned.tip, approved_decision))
    for outcome in reading.outcomes:
        if not isinstance(outcome, (style_ledger.DeclarationNotRead, style_ledger.DeclarationInPlace,
                                    style_ledger.RenameAwaitingApproval)):
            print(follow_line(outcome, reading.tip))
    plan = plan_withdrawal(ledger, {outcome.finding.finding_id: (outcome.finding.state.proposal, style_ledger.DeclarationGone(reading.tip))
                                    for outcome in reading.outcomes if isinstance(outcome, style_ledger.DeclarationRemoved)
                                    and isinstance(outcome.finding.state, style_ledger.Filed)})
    closes: list[Path] = []
    for close in plan.closes:
        print(f"close {close.issue}: {close.reason}")
        if dry_run:
            closes.append(close.issue)
            continue
        try:
            proposal_command("close", str(close.issue), "--reason", close.reason)
            closes.append(close.issue)
        except (style_ledger.LedgerError, Refusal) as error:
            skipped.append(close.issue)
            print(skip_line(close.issue, error))
    if not dry_run:
        # Each decision that holds a followed finding refreshes, not only the ones this run moved, so a run that
        # stopped before its refresh ends on a rerun. A refresh writes only when the decision's bytes change.
        followed = {finding.state.proposal for finding in ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
                    if isinstance(finding.state, style_ledger.Filed) and isinstance(finding.follow, style_ledger.FollowedDeclaration)}
        for issue in sorted((set(plan.refreshes) | {issue for issue in followed if open_decision(issue)})
                            - {close.issue for close in plan.closes} - set(skipped)):
            try:
                # The step prints only its own lines, as a dry run does; the notices of the refresh go to stderr.
                with redirect_stdout(sys.stderr):
                    replace(writer, parent=decision_parent(issue)).refresh(Issue(issue, frontmatter(issue)))
            except (style_ledger.LedgerError, Refusal) as error:
                skipped.append(issue)
                print(skip_line(issue, error))
        if closes:
            renumber()
    return FollowRun(reading, tuple(closes), tuple(skipped))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    filing = sub.add_parser("file")
    _ = filing.add_argument("--proposals", type=Path, required=True)
    _ = filing.add_argument("--parent", required=True)
    ranking = sub.add_parser("rank")
    _ = ranking.add_argument("--proposals", type=Path, required=True)
    linking = sub.add_parser("link-rules")
    _ = linking.add_argument("--proposals", type=Path, required=True)
    _ = sub.add_parser("complete")
    _ = sub.add_parser("candidates")
    showing = sub.add_parser("sites")
    _ = showing.add_argument("--issue", type=Path, required=True)
    reopening = sub.add_parser("reopen")
    targets = reopening.add_mutually_exclusive_group(required=True)
    _ = targets.add_argument("--issue", type=Path)
    _ = targets.add_argument("--all", action="store_true")
    _ = reopening.add_argument("--parent", default="")
    _ = reopening.add_argument("--dry-run", action="store_true")
    # A person who read the code files these built findings again, or keeps them built, without the applied check.
    _ = reopening.add_argument("--finding", action="append", default=[])
    _ = reopening.add_argument("--applied", action="append", default=[])
    _ = reopening.add_argument("--by", default="")
    following = sub.add_parser("follow")
    _ = following.add_argument("--proposals", type=Path, required=True)
    _ = following.add_argument("--dry-run", action="store_true")
    withdrawing = sub.add_parser("withdraw")
    _ = withdrawing.add_argument("--proposals", type=Path, required=True)
    _ = withdrawing.add_argument("--dry-run", action="store_true")
    migration = sub.add_parser("migrate")
    _ = migration.add_argument("--proposals", type=Path, required=True)
    _ = migration.add_argument("--parent", required=True)
    _ = migration.add_argument("--dry-run", action="store_true")
    deduplicating = sub.add_parser("drop-duplicates")
    _ = deduplicating.add_argument("--dry-run", action="store_true")
    dropping = sub.add_parser("drop-candidate")
    _ = dropping.add_argument("--idea", required=True)
    declining = sub.add_parser("decline")
    _ = declining.add_argument("--issue", type=Path, required=True)
    _ = declining.add_argument("--words", required=True)
    unwaiving = sub.add_parser("unwaive")
    _ = unwaiving.add_argument("--proposals", type=Path, required=True)
    _ = unwaiving.add_argument("--id", dest="ids", action="append", required=True)
    _ = unwaiving.add_argument("--by", required=True)
    for command in sub.choices.values():
        _ = command.add_argument("--repo", type=Path, default=Path.cwd())
    args = cast(ProposalArguments, parser.parse_args(argv))
    if args.command == "reopen" and not args.all and not args.parent:
        parser.error("reopen --issue requires --parent")
    read = args.command == "reopen" and bool(args.finding or args.applied)
    if read and args.all:
        parser.error("reopen --finding and --applied require --issue, not --all")
    if args.command == "reopen" and read != bool(args.by.strip()):
        parser.error("reopen --finding and --applied require --by, and --by requires one of them")
    if read and any(not name.strip() for name in (*args.finding, *args.applied)):
        parser.error("reopen --finding and --applied require a finding id")
    if args.command == "unwaive" and any(not name.strip() for name in args.ids):
        parser.error("unwaive --id requires a finding id")
    if args.command == "unwaive" and not args.by.strip():
        parser.error("unwaive --by requires a name")
    try:
        source = (style_ledger.preview_ledger(args.repo) if args.command in ("migrate", "reopen", "withdraw", "follow", "drop-duplicates")
                  and args.dry_run
                  else style_ledger.open_ledger(args.repo))
        with source as ledger:
            if args.command == "file":
                result = file_proposals(ledger, args.proposals.expanduser().resolve(), args.parent)
                if result.skipped:
                    print(f"style_proposals: skipped {len(result.skipped)} decisions", file=sys.stderr)
                    return 1
            elif args.command == "rank":
                rank_proposals(ledger, args.proposals.expanduser().resolve())
            elif args.command == "link-rules":
                link_rules(ledger, args.proposals.expanduser().resolve())
            elif args.command == "decline":
                decline(ledger, args.issue.expanduser().resolve(), args.words)
            elif args.command == "unwaive":
                unwaive(ledger, args.proposals.expanduser().resolve(), tuple(args.ids), style_ledger.WaiveUndoneBy(args.by.strip()))
            elif args.command == "sites":
                sites(ledger, args.issue.expanduser().resolve())
            elif args.command == "candidates":
                candidates(ledger)
            elif args.command == "drop-candidate":
                drop_candidate(ledger, args.idea)
            elif args.command == "migrate":
                migrate(ledger, args.proposals.expanduser().resolve(), args.parent, dry_run=args.dry_run)
            elif args.command == "withdraw":
                # Each refresh keeps the parent its decision names, so the writer takes none.
                skipped = run_withdrawal(decision_writer(ledger, args.proposals.expanduser().resolve(), ""), dry_run=args.dry_run)
                if skipped:
                    print(f"style_proposals: skipped {len(skipped)} decisions", file=sys.stderr)
                    return 1
            elif args.command == "follow":
                writer = decision_writer(ledger, args.proposals.expanduser().resolve(), "")
                follow = run_follow(writer, unfiled_and_filed(ledger), dry_run=args.dry_run)
                print(follow_totals(follow))
                left = follow.count(style_ledger.DeclarationLeft) + follow.sites(style_ledger.NeedsJudgment)
                if follow.skipped:
                    print(f"style_proposals: skipped {len(follow.skipped)} decisions", file=sys.stderr)
                if left:
                    print(f"style_proposals: left {left} findings for a person", file=sys.stderr)
                if follow.skipped or left:
                    return 1
            elif args.command == "drop-duplicates":
                skipped = drop_duplicates(ledger, dry_run=args.dry_run)
                if skipped:
                    print(f"style_proposals: skipped {len(skipped)} decisions", file=sys.stderr)
                    return 1
            elif args.command == "reopen" and args.all:
                sweep = reopen_all(ledger, args.parent, dry_run=args.dry_run)
                if sweep.skipped:
                    print(f"style_proposals: skipped {len(sweep.skipped)} issues", file=sys.stderr)
                if sweep.unchecked_renames:
                    print(f"style_proposals: cannot check {sweep.unchecked_renames} built findings", file=sys.stderr)
                if sweep.skipped or sweep.unchecked_renames:
                    return 1
            elif args.command == "reopen":
                reading = PersonRead(args.by.strip(), tuple(args.finding), tuple(args.applied)) if read else CHECK_ONLY
                unchecked_renames = reopen(ledger, args.issue.expanduser().resolve(), args.parent, reading=reading,
                                           dry_run=args.dry_run)
                if unchecked_renames:
                    print(f"style_proposals: cannot check {len(unchecked_renames)} built findings", file=sys.stderr)
                    return 1
            else:
                complete(ledger)
        return 0
    except style_ledger.LedgerError as error:
        print(f"style_proposals: {' '.join(str(error).splitlines())}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as error:
        print(f"style_proposals: {error}", file=sys.stderr)
        return 2


class ProposalArguments(argparse.Namespace):
    command: str = ""
    repo: Path = Path()
    proposals: Path = Path()
    parent: str = ""
    issue: Path = Path()
    words: str = ""
    idea: str = ""
    dry_run: bool = False
    all: bool = False
    finding: list[str] = []
    applied: list[str] = []
    by: str = ""
    ids: list[str] = []


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
