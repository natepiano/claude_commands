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
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass, field, replace
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
from approved_renames import RenameNotApplied
from add_style import StandingStyleProduction, standing_style_production
from issues import Issue, frontmatter, open_issues, plain
from proposal_issue import (GOAL, INHERITED_FIELDS, STAR, ApprovedDecision, DeclinedDecision, Refusal,
                            UnansweredDecision, decision_answer, fill_ranking, refresh_decision, require_generated_body)
from unit_files import file_text, git, unit_file_holders
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
    """The findings the issue shows: each finding filed to it, but not one a newer verdict withdrew."""
    return tuple(finding for finding in ledger.proposal_findings(issue)
                 if not isinstance(finding.state, style_ledger.Withdrawn))


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
        findings = issue_findings(self.ledger, decision.path)
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


# The close reasons of a decision with no finding left to build.
BUILT_REASON = "built in "
NOTHING_LEFT_REASON = "nothing left to build: "
FINISHED_REASONS = (BUILT_REASON, NOTHING_LEFT_REASON)


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
    """The decision is closed as finished. A finding that joins it reopens it with its approval."""


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
    if closed and not fields.get("reason", [""])[0].startswith(FINISHED_REASONS) and not fields.get("as_built"):
        raise style_ledger.LedgerError("closed decision is not built")
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
    skipped = list(run_withdrawal(writer, dry_run=False))
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
        if decision.path not in skipped and is_decision(decision) and issue_findings(ledger, decision.path):
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
    if not findings or not all(isinstance(item.state, (style_ledger.Built, style_ledger.Waived)) for item in findings):
        return False
    tickets = sorted(ticket["id"] for ticket in ledger.proposal_tickets()
                     if ticket["state"] == "merged" and Path(ticket["proposal"]) == issue)
    if tickets:
        reason = BUILT_REASON + ", ".join(tickets)
    else:
        # Refill waives a finding whose file is gone, so nothing was built.
        commit = ledger.synced_commit()
        paths = tuple(sorted({item.path for item in findings}))
        removed = sorted(set(paths) - files_at(ledger.repo, commit, paths))
        reason = NOTHING_LEFT_REASON + (f"{', '.join(removed)} not in synced commit {commit}" if removed
                                        else "every finding is waived")
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
    for finding_id, outcome in ledger.decline_findings(filed, words).items():
        if isinstance(outcome, style_ledger.InputChangedSinceJudged):
            print(f"not re-recorded {finding_id}: input changed since judged")
    if frontmatter(issue).get("status") != ["closed"]:
        proposal_command("close", str(issue), "--reason", f"declined: {words}")
    renumber()


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
    finding_ids: frozenset[str]


MoveScope = ResumeMigration | FileBuiltAgain


@dataclass(frozen=True)
class MovePlan:
    """What a move of old issues' findings into their decisions writes. Making the plan writes nothing."""
    sources: tuple[LegacyIssue, ...]
    moves: tuple[DecisionMove, ...]
    closes: tuple[OldIssueClose, ...]
    # Built findings that are not in the code. The move files them again before it moves them.
    refile: frozenset[str]
    # Each old issue that keeps its findings, with the reason.
    refusals: tuple[str, ...]
    # The decision of each finding that the move takes from an old issue.
    decisions: Mapping[str, Path]
    # The refused and kept lines of the old issues, in order.
    issue_lines: tuple[str, ...]


def plan_moves(ledger: style_ledger.Ledger, proposals: Path, issues: list[Issue], scope: MoveScope) -> MovePlan:
    """Plan the move of the findings scope takes from each old issue into their decisions, and each open issue's close."""
    all_findings = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
    open_tickets = tuple(ticket for ticket in ledger.proposal_tickets()
                         if ticket["state"] not in style_ledger.FINAL_TICKET_STATES)
    holding = {Path(ticket["proposal"]): ticket for ticket in open_tickets}
    open_ids = {ticket["id"] for ticket in open_tickets}
    refile = scope.finding_ids if isinstance(scope, FileBuiltAgain) else frozenset[str]()
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
    for key, findings in sorted(groups.items()):
        issue = find_decision(proposals, key)
        join: NewDecision | JoinAction
        if issue.exists():
            join = join_action(issue)
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
    closes: list[OldIssueClose] = []
    for source in sources:
        # A closed old issue that reopen repairs keeps its close reason.
        if source.issue.value("status") != "open":
            continue
        names = sorted({decisions[finding.finding_id].name for finding in source.owned})
        closes.append(OldIssueClose(source.issue.path, "merged into " + ", ".join(names) if names else "no filed findings"))
    return MovePlan(tuple(sources), tuple(moves), tuple(closes), refile, tuple(refusals), decisions, tuple(lines))


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
    if not dry_run:
        if plan.refile:
            ledger.return_to_filed(sorted(plan.refile))
        for source in plan.sources:
            if source.owned:
                ledger.move_issue_findings(source.issue.path, {
                    finding.finding_id: plan.decisions[finding.finding_id] for finding in source.owned})
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


def reopen(ledger: style_ledger.Ledger, issue: Path, parent: str, *, dry_run: bool = False) -> None:
    """Set each built rename of the issue that is not in the synced code back to filed, in an approved decision.

    Every refusal comes before the first write.
    """
    if not file_unapplied_again(ledger, issue, parent, dry_run=dry_run):
        print("nothing to reopen: every built finding is applied")
    elif not dry_run:
        renumber()


def old_issue_parent(issue: Path, given: str) -> str:
    """The parent that the old issue's Parent line names, else the given parent."""
    match = PARENT_LINE.search(issue.read_text())
    if match is not None:
        return match[1]
    if not given:
        raise style_ledger.LedgerError("no Parent line; pass --parent")
    return given


def reopen_all(ledger: style_ledger.Ledger, given_parent: str, *, dry_run: bool = False) -> tuple[Path, ...]:
    """Reopen each approved decision and old issue that holds a built finding, then print the totals.

    An old issue takes the parent its Parent line names, else given_parent. Each issue that the sweep
    leaves out or reopen refuses prints a skip line and the others go on; returns those issues.
    """
    built = {finding.state.proposal for finding in ledger.held_findings(style_ledger.HeldFindingsInState("built"))
             if isinstance(finding.state, style_ledger.Built)}
    reopened = 0
    filed_again = 0
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
            unbuilt = file_unapplied_again(ledger, issue, parent, dry_run=dry_run)
        except style_ledger.LedgerError as error:
            skipped.append(issue)
            print(skip_line(issue, error))
            continue
        reopened += bool(unbuilt)
        filed_again += len(unbuilt)
    if reopened and not dry_run:
        renumber()
    print(f"totals: reopen {reopened}, filed again {filed_again}, skipped {len(skipped)}")
    return tuple(skipped)


def file_unapplied_again(ledger: style_ledger.Ledger, issue: Path, parent: str, *,
                         dry_run: bool) -> tuple[style_ledger.HeldFinding, ...]:
    """File each built rename of the issue that is not in the synced code again, print a line for each and return them.

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
    # A run that stopped before the move leaves an old issue's findings filed to it.
    unbuilt = tuple(finding for finding in issue_findings(ledger, issue)
                    if (isinstance(finding.state, style_ledger.Built)
                        or not decision and isinstance(finding.state, style_ledger.Filed))
                    and isinstance(ledger.applied_check(finding, commit), RenameNotApplied))
    if not unbuilt:
        return ()
    built = frozenset(finding.finding_id for finding in unbuilt if isinstance(finding.state, style_ledger.Built))
    if decision:
        decisions = {finding.finding_id: issue for finding in unbuilt}
        if not dry_run:
            # Open the decision first: a stop before the findings are filed again leaves it built, so complete closes it.
            if source.value("status") != "open":
                proposal_command("reopen", str(issue))
            ledger.return_to_filed(sorted(built))
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
        # Reopen prints only its own lines; the notices of a new decision go to stderr.
        with redirect_stdout(sys.stderr):
            run_moves(ledger, issue.parent, parent, plan, lambda _: None, dry_run=dry_run)
    for finding in unbuilt:
        print(f"filed again {finding.finding_id} in {decisions[finding.finding_id]}: "
              + f"{finding.before} -> {finding.after} not applied in {finding.path}")
    return unbuilt


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


def plan_withdrawal(ledger: style_ledger.Ledger, withdrawal: style_ledger.Withdrawal) -> WithdrawalPlan:
    """Plan the close or refresh of each open issue that holds a withdrawn finding, as if withdrawal were written.

    An issue closes only when every one of its findings is withdrawn; one that keeps another finding is refreshed,
    and complete closes it when the rest are resolved. Findings that an earlier run withdrew count too, so a run
    that stopped before its closes ends on a rerun.
    """
    verdicts = {item.finding.finding_id: item.verdict.judgment_id for item in withdrawal.withdrawn}
    issues = {item.finding.state.proposal for item in withdrawal.withdrawn if isinstance(item.finding.state, style_ledger.Filed)}
    issues.update(finding.state.proposal for finding in ledger.held_findings(style_ledger.HeldFindingsInState("withdrawn"))
                  if isinstance(finding.state, style_ledger.Withdrawn))
    closes: list[EmptiedDecisionClose] = []
    refreshes: list[Path] = []
    for issue in sorted(issues):
        if not issue.is_file():
            continue
        fields = frontmatter(issue)
        if fields.get("status") != ["open"]:
            continue
        findings = ledger.proposal_findings(issue)
        if any(not isinstance(finding.state, style_ledger.Withdrawn) and finding.finding_id not in verdicts for finding in findings):
            if is_decision(Issue(issue, fields)):
                refreshes.append(issue)
            continue
        rows = {finding.finding_id: finding.state.judgment_id for finding in findings
                if isinstance(finding.state, style_ledger.Withdrawn)}
        rows.update((finding.finding_id, verdicts[finding.finding_id]) for finding in findings if finding.finding_id in verdicts)
        withdrawn = ", ".join(f"{finding_id} (judgment {row})" for finding_id, row in sorted(rows.items()))
        closes.append(EmptiedDecisionClose(issue, f"{NOTHING_LEFT_REASON}withdrawn by an exception: {withdrawn}"))
    return WithdrawalPlan(tuple(closes), tuple(refreshes))


def superseded_line(finding: style_ledger.HeldFinding, verdict: style_ledger.SupersedingException) -> str:
    return (f"{finding.finding_id} {finding.rule} {finding.path}:{finding.line}: "
            + f"judgment {verdict.judgment_id} exception under {verdict.rule_blob[:8]}")


def run_withdrawal(writer: DecisionWriter, *, dry_run: bool) -> tuple[Path, ...]:
    """Withdraw each filed finding that an exception under its rule's newer text supersedes, then close or refresh its decision.

    Print one line per withdrawn and kept finding and per closed decision, then the totals. A dry run prints the
    same lines and writes nothing. A refresh keeps the parent that the decision names. A decision that cannot
    close or refresh prints a skip line and the others go on; returns those decisions.
    """
    ledger = writer.ledger
    withdrawal = (ledger.superseded_findings(writer.definitions) if dry_run
                  else ledger.withdraw_superseded(writer.definitions))
    plan = plan_withdrawal(ledger, withdrawal)
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
    withdrawing = sub.add_parser("withdraw")
    _ = withdrawing.add_argument("--proposals", type=Path, required=True)
    _ = withdrawing.add_argument("--dry-run", action="store_true")
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
    if args.command == "reopen" and not args.all and not args.parent:
        parser.error("reopen --issue requires --parent")
    try:
        source = (style_ledger.preview_ledger(args.repo) if args.command in ("migrate", "reopen", "withdraw") and args.dry_run
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
            elif args.command == "withdraw":
                # Each refresh keeps the parent its decision names, so the writer takes none.
                skipped = run_withdrawal(decision_writer(ledger, args.proposals.expanduser().resolve(), ""), dry_run=args.dry_run)
                if skipped:
                    print(f"style_proposals: skipped {len(skipped)} decisions", file=sys.stderr)
                    return 1
            elif args.command == "reopen" and args.all:
                skipped = reopen_all(ledger, args.parent, dry_run=args.dry_run)
                if skipped:
                    print(f"style_proposals: skipped {len(skipped)} issues", file=sys.stderr)
                    return 1
            elif args.command == "reopen":
                reopen(ledger, args.issue.expanduser().resolve(), args.parent, dry_run=args.dry_run)
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
