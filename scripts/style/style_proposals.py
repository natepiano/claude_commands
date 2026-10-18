#!/usr/bin/env python3
"""Batch held style findings into issues and finish approved or declined proposals."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

import coverage_report
import rules
import style_ledger
from add_style import StandingStyleProduction, standing_style_production
from issues import Issue, frontmatter, open_issues
from proposal_issue import STAR
from unit_files import unit_file_holders


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


@dataclass(frozen=True)
class NoApprovedProposal:
    """There is no approved open style issue for this unit."""


def approved_issue(folder: Path, unit: str, ledger: style_ledger.Ledger) -> Issue | NoApprovedProposal:
    used = {ticket["proposal"] for ticket in ledger.proposal_tickets() if ticket["state"] != "dropped"}
    candidates = (issue for issue in open_issues(folder, "style")
                  if issue.value("unit") == unit and issue.value("plan") and str(issue.path) not in used)
    ordered = sorted(candidates, key=lambda issue: (issue.value("date_created"), issue.path.name))
    return ordered[0] if ordered else NoApprovedProposal()


def issue_findings(ledger: style_ledger.Ledger, issue: Path) -> tuple[style_ledger.HeldFinding, ...]:
    by_id = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
    return _issue_findings(by_id, issue)


def _issue_findings(by_id: Mapping[str, style_ledger.HeldFinding], issue: Path) -> tuple[style_ledger.HeldFinding, ...]:
    ids = frontmatter(issue).get("style_findings", [])
    if not ids or any(finding_id not in by_id for finding_id in ids):
        raise style_ledger.LedgerError(f"{issue}: unknown or absent style_findings")
    return tuple(by_id[finding_id] for finding_id in ids)


def repository_name(repo: Path) -> str:
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise style_ledger.LedgerError(result.stderr.strip())
    return Path(result.stdout.strip()).parent.name


def style_slug(repo: Path) -> str:
    return repository_name(repo) + "-style"


def crate_for(ledger: style_ledger.Ledger, path: str) -> str:
    return _crate_for(_workspace_directories(ledger), path)


def _workspace_directories(ledger: style_ledger.Ledger) -> tuple[str, ...]:
    return tuple(member.directory for member in coverage_report.workspace(ledger, ledger.summary().synced_tip))


def _crate_for(directories: tuple[str, ...], path: str) -> str:
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


def batch_name(label: str, findings: tuple[style_ledger.HeldFinding, ...]) -> str:
    digest = hashlib.sha256("\n".join(sorted(finding.finding_id for finding in findings)).encode()).hexdigest()[:8]
    label = re.sub(r"[^\w .-]", "-", label).strip(" .-") or "crate"
    return f"style {label[:32]} {digest}.md"


def proposed_ranking(kind: style_ledger.FindingKind, finding_count: int) -> str:
    effort = 1 if finding_count <= 3 else 2 if finding_count <= 15 else 3
    impact = 1 if kind == "rename" else 2
    return (f"goal 1 - Ship Hana · alignment {STAR * 2} · impact {STAR * impact} "
            + f"· urgency {STAR} · effort {STAR * effort}")


def proposal_kind(finding: style_ledger.HeldFinding, definition: rules.Rule) -> style_ledger.FindingKind:
    return "restructure" if definition.tier == "D" or definition.mode_propose else finding.kind


def file_proposals(ledger: style_ledger.Ledger, proposals: Path, parent: str) -> tuple[Path, ...]:
    definitions = {rule.rule_id: rule for rule in rules.load_rules(ledger.repo)}
    directories = _workspace_directories(ledger)
    waiting = ledger.held_findings(style_ledger.UnfiledFindingsFromOpenTickets())
    open_ticket_counts: dict[str, int] = defaultdict(int)
    for finding in waiting:
        open_ticket_counts[finding.ticket] += 1
    for ticket, count in sorted(open_ticket_counts.items()):
        print(f"held for open ticket {ticket}: {count} findings")
    waiting_ids = {finding.finding_id for finding in waiting}
    valid: list[style_ledger.HeldFinding] = []
    for finding in ledger.held_findings(style_ledger.HeldFindingsInState("unfiled")):
        if finding.finding_id in waiting_ids:
            continue
        definition = definitions.get(finding.rule.removesuffix("#uncovered"))
        if definition is None or definition.blob != finding.rule_blob:
            print(f"skipped {finding.finding_id}: rule changed since judged")
        else:
            valid.append(finding)
    # Recover a create that reached disk before file_held, including partially marked batches.
    for issue_path in sorted(proposals.glob("*.md")):
        fields = frontmatter(issue_path)
        if "style" not in fields.get("tags", []):
            continue
        identities = set(fields.get("style_findings", []))
        recovered = [finding for finding in valid if finding.finding_id in identities]
        for finding in recovered:
            ledger.file_held(finding.finding_id, issue_path)
        valid = [finding for finding in valid if finding.finding_id not in identities]
    batches: dict[tuple[style_ledger.FindingKind, str, str], list[style_ledger.HeldFinding]] = defaultdict(list)
    for finding in valid:
        definition = definitions[finding.rule.removesuffix("#uncovered")]
        kind = proposal_kind(finding, definition)
        crate = _crate_for(directories, finding.path)
        batches[(kind, "" if kind == "restructure" else finding.rule, crate)].append(finding)
    if not batches:
        return ()
    standing = standing_style_production(ledger.repo)
    holders: dict[str, frozenset[str]] = (
        unit_file_holders(ledger.repo, standing.doc, ledger) if isinstance(standing, StandingStyleProduction) else {}
    )
    written: list[Path] = []
    for (kind, rule, crate), items in sorted(batches.items()):
        findings = tuple(items)
        label = (PurePosixPath(rule.removesuffix("#uncovered")).stem if rule else
                 repository_name(ledger.repo) if crate == "." else PurePosixPath(crate).name)
        issue = proposals / batch_name(label, findings)
        body = [f"# Style {kind}: {label}", "", f"Parent: {parent}", "", "Recommended answer: approve the described change; schedule it with any live units holding these files.", ""]
        for finding in findings:
            live = ", ".join(sorted(unit for unit, paths in holders.items() if finding.path in paths)) or "none"
            body.extend([f"## {finding.finding_id}", f"Rule: {finding.rule}",
                         f"Site: {finding.path}:{finding.line} — {finding.text}", f"Live units holding file: {live}",
                         "", "Before:", "```", finding.before, "```", "After:", "```", finding.after, "```", ""])
        with tempfile.TemporaryDirectory(prefix="style-proposal-") as scratch:
            body_file = Path(scratch) / "body.md"
            _ = body_file.write_text("\n".join(body))
            project = issue_project(ledger.repo, crate, proposals.parent)
            proposal_command("create", str(issue), "--project", project.project, "--short", project.short,
                             "--proposed-by", style_slug(ledger.repo),
                             "--proposed-ranking", proposed_ranking(kind, len(findings)),
                             "--extra-tags", "style", "--findings", ",".join(finding.finding_id for finding in findings),
                             "--body-file", str(body_file))
        for finding in findings:
            ledger.file_held(finding.finding_id, issue)
        written.append(issue)
        print(f"filed {issue}")
    return tuple(written)


def rank_proposals(ledger: style_ledger.Ledger, proposals: Path) -> None:
    definitions = {rule.rule_id: rule for rule in rules.load_rules(ledger.repo)}
    directories = _workspace_directories(ledger)
    by_id = {finding.finding_id: finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())}
    ranked = 0
    for issue in open_issues(proposals, "style"):
        try:
            findings = _issue_findings(by_id, issue.path)
            kinds: set[style_ledger.FindingKind] = set()
            crates: set[str] = set()
            for finding in findings:
                rule = finding.rule.removesuffix("#uncovered")
                if rule not in definitions:
                    raise style_ledger.LedgerError(f"unknown rule {rule}")
                kinds.add(proposal_kind(finding, definitions[rule]))
                crates.add(_crate_for(directories, finding.path))
            if len(kinds) != 1 or len(crates) != 1:
                raise style_ledger.LedgerError("findings do not form one proposal batch")
            kind: style_ledger.FindingKind = next(iter(kinds))
            text = issue.path.read_bytes().decode("utf-8")
            updated = re.sub(r"^Proposed ranking:[ \t]*(?=\r?$)",
                             lambda match: match[0].rstrip(" \t") + " " + proposed_ranking(kind, len(findings)),
                             text, count=1, flags=re.MULTILINE)
            if kind == "restructure" and crates == {"."}:
                updated = re.sub(r"^# Style restructure: (?=\r?$)",
                                 lambda match: match[0] + repository_name(ledger.repo),
                                 updated, count=1, flags=re.MULTILINE)
            if updated != text:
                _ = issue.path.write_bytes(updated.encode("utf-8"))
                ranked += 1
                print(f"ranked {issue.path}")
        except (style_ledger.LedgerError, OSError, UnicodeError) as error:
            print(f"skipped {issue.path}: {error}")
    print(f"ranked {ranked} issues")


def complete(ledger: style_ledger.Ledger) -> None:
    merged = False
    for ticket in ledger.proposal_tickets():
        if ticket["state"] != "merged":
            continue
        merged = True
        issue = Path(ticket["proposal"])
        if frontmatter(issue).get("status") == ["open"]:
            proposal_command("close", str(issue), "--reason", f"built in {ticket['id']}")
    if merged:
        renumber()


def decline(ledger: style_ledger.Ledger, issue: Path, words: str) -> None:
    if frontmatter(issue).get("status") == ["closed"]:
        renumber()
        return
    for finding in issue_findings(ledger, issue):
        result = ledger.decline_held(finding.finding_id, words)
        if isinstance(result, style_ledger.InputChangedSinceJudged):
            print(f"not re-recorded {finding.finding_id}: input changed since judged")
    proposal_command("close", str(issue), "--reason", f"declined: {words}")
    renumber()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    filing = sub.add_parser("file")
    _ = filing.add_argument("--proposals", type=Path, required=True)
    _ = filing.add_argument("--parent", required=True)
    ranking = sub.add_parser("rank")
    _ = ranking.add_argument("--proposals", type=Path, required=True)
    _ = sub.add_parser("complete")
    declining = sub.add_parser("decline")
    _ = declining.add_argument("--issue", type=Path, required=True)
    _ = declining.add_argument("--words", required=True)
    for command in sub.choices.values():
        _ = command.add_argument("--repo", type=Path, default=Path.cwd())
    args = cast(ProposalArguments, parser.parse_args(argv))
    try:
        with style_ledger.open_ledger(args.repo) as ledger:
            if args.command == "file":
                _ = file_proposals(ledger, args.proposals.expanduser().resolve(), args.parent)
            elif args.command == "rank":
                rank_proposals(ledger, args.proposals.expanduser().resolve())
            elif args.command == "decline":
                decline(ledger, args.issue.expanduser().resolve(), args.words)
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
