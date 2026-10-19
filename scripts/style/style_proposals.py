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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

import coverage_report
import rules
import style_ledger
import style_rules
from add_style import StandingStyleProduction, standing_style_production
from issues import Issue, frontmatter, open_issues, plain
from proposal_issue import (GOAL, INHERITED_FIELDS, STAR, ApprovedDecision, DeclinedDecision, Refusal,
                            decision_answer, fill_ranking, refresh_decision, require_generated_body)
from unit_files import unit_file_holders
from user_zone import user_zone


def proposal_command(*arguments: str) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "production/proposal_issue.py"), *arguments],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise style_ledger.LedgerError(result.stderr.strip() or result.stdout.strip())


def renumber() -> None:
    result = subprocess.run([sys.executable, str(SCRIPTS / "prioritize/renumber.py"), "--apply"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise style_ledger.LedgerError(result.stderr.strip() or result.stdout.strip())


def is_decision(issue: Issue) -> bool:
    return (all(issue.value(key) for key in ("style_rule", "style_crate", "style_rule_blob"))
            and {"proposal", "style"} <= set(issue.fields.get("tags", [])))


def approved_decisions(folder: Path, unit: str) -> tuple[Issue, ...]:
    return tuple(sorted((issue for issue in open_issues(folder, "style")
                         if is_decision(issue) and isinstance(decision_answer(issue), ApprovedDecision)
                         and issue.value("unit") == unit),
                        key=lambda issue: (issue.value("date_created"), issue.path.name)))


def issue_findings(ledger: style_ledger.Ledger, issue: Path) -> tuple[style_ledger.HeldFinding, ...]:
    return ledger.proposal_findings(issue)


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


def batch_name(key: DecisionKey) -> str:
    label = UNSAFE_NAME.sub("-", PurePosixPath(key.rule).stem).strip(" .-") or "rule"
    # The root crate has no crate part, so no crate path can give its name.
    if key.crate != ".":
        crate = UNSAFE_NAME.sub("-", key.crate).strip(" .-")
        # Two crate paths can give one safe text, so a changed path adds a hash of the path.
        # A plain name cannot hold the parentheses, so no plain path gives a hashed name.
        label += f" {crate}" if crate == key.crate else f" {crate} ({hashlib.sha256(key.crate.encode()).hexdigest()[:8]})"
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
                f"Findings: {len(findings)}; files: {len({finding.path for finding in findings})}", ""]
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

    def create(self, key: DecisionKey, findings: tuple[style_ledger.HeldFinding, ...],
               inherited: Mapping[str, str]) -> Path:
        issue = self.proposals / batch_name(key)
        project = issue_project(self.ledger.repo, key.crate, self.proposals.parent)
        ranking = proposed_ranking(decision_kind(findings, self.definitions), len(findings), self.goal(project.project))
        with tempfile.TemporaryDirectory(prefix="style-decision-") as scratch:
            body = Path(scratch) / "body.md"
            _ = body.write_text(self.text(key, findings))
            proposal_command("create", str(issue), "--project", project.project, "--short", project.short,
                             "--proposed-by", style_slug(self.ledger.repo), "--extra-tags", "style",
                             "--rule", key.rule, "--crate", key.crate, "--rule-blob", key.blob,
                             "--count", str(len(findings)), "--body-file", str(body),
                             "--proposed-ranking", ranking, "--initial-fields", json.dumps(dict(inherited)),
                             "--rules", ",".join(self.linker.urls(findings)))
        return issue

    def refresh(self, decision: Issue) -> None:
        """Write the decision's count, examples and open ranking only when its bytes change."""
        key = DecisionKey(decision.value("style_rule"), decision.value("style_crate"), decision.value("style_rule_blob"))
        findings = self.ledger.proposal_findings(decision.path)
        ranking = proposed_ranking(decision_kind(findings, self.definitions), len(findings),
                                   self.goal(plain(decision.value("project"))))
        _ = refresh_decision(decision.path, len(findings), self.text(key, findings), ranking,
                             datetime.now(user_zone()).date())


def decision_writer(ledger: style_ledger.Ledger, proposals: Path, parent: str) -> DecisionWriter:
    standing = standing_style_production(ledger.repo)
    holders = unit_file_holders(ledger.repo, standing.doc, ledger) if isinstance(standing, StandingStyleProduction) else {}
    return DecisionWriter(ledger, proposals, parent, {rule.rule_id: rule for rule in rules.load_rules(ledger.repo)},
                          holders, RuleLinker(ledger.repo), repository_name(ledger.repo))


def find_decision(proposals: Path, key: DecisionKey) -> Path:
    issue = proposals / batch_name(key)
    if issue.exists():
        fields = frontmatter(issue)
        if any(fields.get(name) != [value] for name, value in (
            ("style_rule", key.rule), ("style_crate", key.crate), ("style_rule_blob", key.blob),
        )):
            raise style_ledger.LedgerError(f"{issue}: different decision key")
    return issue


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
    """The decision is closed as built. A finding that joins it reopens it with its approval."""


@dataclass(frozen=True)
class JoinOpenDecision:
    """The decision is open. A finding joins it with no change to its answer."""


JoinAction = WaiveWithAnswer | ReopenBuiltDecision | JoinOpenDecision


def join_action(issue: Path) -> JoinAction:
    """What the decision's answer does to findings that join it. Refuse a decision they cannot join."""
    fields = frontmatter(issue)
    answer = decision_answer(Issue(issue, fields))
    if isinstance(answer, DeclinedDecision):
        return WaiveWithAnswer(answer.words)
    closed = fields.get("status") == ["closed"]
    if closed and not fields.get("reason", [""])[0].startswith("built in ") and not fields.get("as_built"):
        raise style_ledger.LedgerError("closed decision is not built")
    require_generated_body(issue)
    return ReopenBuiltDecision() if closed else JoinOpenDecision()


def apply_answer(ledger: style_ledger.Ledger, issue: Path, action: JoinAction, finding_ids: tuple[str, ...]) -> None:
    """Waive the joining findings of a declined decision, or reopen a built one."""
    if isinstance(action, WaiveWithAnswer):
        _ = ledger.decline_findings(finding_ids, action.words)
    elif isinstance(action, ReopenBuiltDecision):
        proposal_command("reopen", str(issue))


def file_decision(writer: DecisionWriter, key: DecisionKey, items: tuple[style_ledger.HeldFinding, ...]) -> bool:
    """File the findings to their decision. True when they are filed, False when the answer waived them."""
    issue = find_decision(writer.proposals, key)
    if not issue.exists():
        issue = writer.create(key, items, {})
    action = join_action(issue)
    apply_answer(writer.ledger, issue, action, tuple(finding.finding_id for finding in items))
    if isinstance(action, WaiveWithAnswer):
        return False
    for finding in items:
        writer.ledger.file_held(finding.finding_id, issue)
    return True


def file_proposals(ledger: style_ledger.Ledger, proposals: Path, parent: str) -> FilingResult:
    writer = decision_writer(ledger, proposals, parent)
    directories = workspace_directories(ledger)
    waiting = ledger.held_findings(style_ledger.UnfiledFindingsFromOpenTickets())
    for ticket, count in sorted(Counter(finding.ticket for finding in waiting).items()):
        print(f"held for open ticket {ticket}: {count} findings")
    waiting_ids = {finding.finding_id for finding in waiting}
    batches: dict[DecisionKey, list[style_ledger.HeldFinding]] = defaultdict(list)
    for finding in ledger.held_findings(style_ledger.HeldFindingsInState("unfiled")):
        if finding.finding_id in waiting_ids:
            continue
        definition = writer.definitions.get(finding.rule.removesuffix("#uncovered"))
        if definition is None or definition.blob != finding.rule_blob:
            print(f"skipped {finding.finding_id}: rule changed since judged")
        else:
            batches[decision_key(finding, directories)].append(finding)
    written: list[Path] = []
    skipped: list[Path] = []
    for key, items in sorted(batches.items()):
        issue = proposals / batch_name(key)
        try:
            if file_decision(writer, key, tuple(items)):
                written.append(issue)
                print(f"filed {issue}")
        except (style_ledger.LedgerError, Refusal) as error:
            skipped.append(issue)
            print(skip_line(issue, error))
    # Also finish a refresh interrupted after all ledger moves reached disk.
    # Only an open decision can get new findings, so a closed one needs no repair.
    for decision in open_issues(proposals, "style"):
        if decision.path not in skipped and is_decision(decision) and ledger.proposal_findings(decision.path):
            try:
                writer.refresh(decision)
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
        except (style_ledger.LedgerError, OSError, UnicodeError) as error:
            print(f"skipped {issue.path}: {error}")
    print(f"ranked {ranked} issues")


def complete(ledger: style_ledger.Ledger) -> None:
    merged: dict[Path, list[str]] = defaultdict(list)
    for ticket in ledger.proposal_tickets():
        if ticket["state"] == "merged":
            merged[Path(ticket["proposal"])].append(ticket["id"])
    for issue, tickets in sorted(merged.items()):
        findings = issue_findings(ledger, issue)
        if findings and all(isinstance(item.state, (style_ledger.Built, style_ledger.Waived)) for item in findings):
            if frontmatter(issue).get("status") == ["open"]:
                proposal_command("close", str(issue), "--reason", "built in " + ", ".join(sorted(tickets)))
    if merged:
        renumber()


def decline(ledger: style_ledger.Ledger, issue: Path, words: str) -> None:
    filed = tuple(finding.finding_id for finding in issue_findings(ledger, issue)
                  if isinstance(finding.state, style_ledger.Filed))
    for finding_id, outcome in ledger.decline_findings(filed, words).items():
        if isinstance(outcome, style_ledger.InputChangedSinceJudged):
            print(f"not re-recorded {finding_id}: input changed since judged")
    if decision_answer(Issue(issue, frontmatter(issue))) != DeclinedDecision(words):
        proposal_command("decline-answer", str(issue), "--words", words)
    if frontmatter(issue).get("status") != ["closed"]:
        proposal_command("close", str(issue), "--reason", f"declined: {words}")
    renumber()


def state_name(finding: style_ledger.HeldFinding) -> str:
    return ("filed" if isinstance(finding.state, style_ledger.Filed) else
            "built" if isinstance(finding.state, style_ledger.Built) else
            "waived" if isinstance(finding.state, style_ledger.Waived) else "unfiled")


def sites(ledger: style_ledger.Ledger, issue: Path) -> None:
    for finding in issue_findings(ledger, issue):
        print(f"{finding.finding_id} {state_name(finding)} {finding.path}:{finding.line}\nBefore: {finding.before}\nAfter: {finding.after}")


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


def migrate(ledger: style_ledger.Ledger, proposals: Path, parent: str, *, dry_run: bool = False) -> None:
    all_findings = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
    open_tickets = tuple(ticket for ticket in ledger.proposal_tickets()
                         if ticket["state"] not in style_ledger.FINAL_TICKET_STATES)
    holding = {Path(ticket["proposal"]): ticket for ticket in open_tickets}
    open_ids = {ticket["id"] for ticket in open_tickets}
    directories = workspace_directories(ledger)
    sources: list[LegacyIssue] = []
    keys: dict[str, DecisionKey] = {}
    origin: dict[str, Issue] = {}
    moved_before: set[str] = set()
    for issue in open_issues(proposals, "style"):
        identities = tuple(dict.fromkeys(issue.fields.get("style_findings", [])))
        if not identities:
            continue
        if issue.path in holding:
            ticket = holding[issue.path]
            print(f"refused {issue.path}: live build ticket {ticket['id']} ({ticket['state']})")
            continue
        if any(identity not in all_findings for identity in identities):
            print(f"refused {issue.path}: unknown findings")
            continue
        listed = tuple(all_findings[identity] for identity in identities)
        if any(isinstance(item.build_ticket, style_ledger.BuildTicket) and
               item.build_ticket.ticket in open_ids for item in listed):
            print(f"refused {issue.path}: live build ticket")
            continue
        # An earlier run that stopped moved these findings from this issue.
        moved = ledger.moved_findings(issue.path)
        owned: list[style_ledger.HeldFinding] = []
        for finding in listed:
            if finding.finding_id in moved or (isinstance(finding.state, style_ledger.Filed)
                                               and finding.state.proposal == issue.path):
                owned.append(finding)
                keys[finding.finding_id] = decision_key(finding, directories)
                origin[finding.finding_id] = issue
            else:
                print(f"kept {finding.finding_id}: {state_name(finding)}, not filed to {issue.path}")
        moved_before.update(moved)
        sources.append(LegacyIssue(issue, tuple(owned)))
    groups: dict[DecisionKey, list[style_ledger.HeldFinding]] = defaultdict(list)
    for source in sources:
        for finding in source.owned:
            groups[keys[finding.finding_id]].append(finding)
    writer = decision_writer(ledger, proposals, parent)
    decisions: dict[DecisionKey, Path] = {}
    actions: dict[DecisionKey, JoinAction] = {}
    pending: dict[DecisionKey, tuple[str, ...]] = {}
    created = moved_count = closed = 0
    for key, findings in sorted(groups.items()):
        issue = find_decision(proposals, key)
        action: JoinAction = JoinOpenDecision()
        if not issue.exists():
            created += 1
            print(f"create {issue}")
            if not dry_run:
                origins = sorted({origin[finding.finding_id].path: origin[finding.finding_id] for finding in findings}.values(),
                                 key=lambda item: item.path)
                inherited = {name: origins[0].value(name) for name in sorted(INHERITED_FIELDS)
                             if origins[0].value(name) and all(item.value(name) == origins[0].value(name) for item in origins)}
                _ = writer.create(key, tuple(findings), inherited)
        else:
            action = join_action(issue)
        count = sum(finding.finding_id not in moved_before for finding in findings)
        moved_count += count
        print(f"move {count} findings to {issue}")
        # A finding still filed has not yet taken the decision's answer.
        pending[key] = tuple(finding.finding_id for finding in findings if isinstance(finding.state, style_ledger.Filed))
        if isinstance(action, WaiveWithAnswer):
            print(f"waive {len(pending[key])} findings in {issue}: declined: {action.words}")
        elif isinstance(action, ReopenBuiltDecision):
            print(f"reopen {issue}")
        decisions[key] = issue
        actions[key] = action
    if not dry_run:
        for source in sources:
            if source.owned:
                ledger.move_issue_findings(source.issue.path, {
                    finding.finding_id: decisions[keys[finding.finding_id]] for finding in source.owned})
        for key, issue in sorted(decisions.items()):
            apply_answer(ledger, issue, actions[key], pending[key])
        for issue in sorted(set(decisions.values())):
            writer.refresh(Issue(issue, frontmatter(issue)))
    for source in sources:
        names = sorted({decisions[keys[finding.finding_id]].name for finding in source.owned})
        reason = "merged into " + ", ".join(names) if names else "no filed findings"
        print(f"close {source.issue.path}: {reason}")
        closed += 1
        if not dry_run:
            proposal_command("close", str(source.issue.path), "--reason", reason)
    print(f"totals: create {created}, move {moved_count}, close {closed}")


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
    migration = sub.add_parser("migrate")
    _ = migration.add_argument("--proposals", type=Path, required=True)
    _ = migration.add_argument("--parent", required=True)
    _ = migration.add_argument("--dry-run", action="store_true")
    dropping = sub.add_parser("drop-candidate")
    _ = dropping.add_argument("--idea", required=True)
    declining = sub.add_parser("decline")
    _ = declining.add_argument("--issue", type=Path, required=True)
    _ = declining.add_argument("--words", required=True)
    for command in sub.choices.values():
        _ = command.add_argument("--repo", type=Path, default=Path.cwd())
    args = cast(ProposalArguments, parser.parse_args(argv))
    try:
        source = (style_ledger.preview_ledger(args.repo) if args.command == "migrate" and args.dry_run
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
            elif args.command == "sites":
                sites(ledger, args.issue.expanduser().resolve())
            elif args.command == "candidates":
                candidates(ledger)
            elif args.command == "drop-candidate":
                drop_candidate(ledger, args.idea)
            elif args.command == "migrate":
                migrate(ledger, args.proposals.expanduser().resolve(), args.parent, dry_run=args.dry_run)
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
