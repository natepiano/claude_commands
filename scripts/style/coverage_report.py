#!/usr/bin/env python3
"""Print style coverage at the synced tip and remember the last report."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "lib"))

import rules
import style_ledger
from user_zone import user_zone


class RuleSnapshot(TypedDict):
    done: bool
    cells_total: int


class ReportSnapshot(TypedDict):
    rules: dict[str, RuleSnapshot]
    proposals: list[str]
    files_by_day: dict[str, int]


@dataclass(frozen=True)
class FirstReport:
    """There is no earlier report to compare."""


@dataclass(frozen=True)
class PreviousReport:
    rules: dict[str, RuleSnapshot]
    proposals: frozenset[str]
    files_by_day: dict[str, int]


@dataclass(frozen=True)
class StyleProduction:
    slug: str
    branch_point: str
    units: tuple[str, ...]


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise style_ledger.LedgerError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def production(ledger: style_ledger.Ledger, tip: str) -> StyleProduction:
    main = Path(git(ledger.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    slug = main.name + "-style"
    doc = main.parent / slug / "docs" / f"{slug}-production.md"
    units = tuple(f"{slug}-{family}-unit" for family in ("modules", "names", "values", "types"))
    if doc.is_file():
        text = doc.read_text(encoding="utf-8")
        branch = re.search(r"^\s*-?\s*\*\*Branch point:\*\*\s*(.+)$", text, re.MULTILINE)
        if branch is None:
            raise style_ledger.LedgerError(f"{doc}: missing Branch point")
        rows = text.split("## Units", 1)[-1].split("\n## ", 1)[0]
        declared = tuple(match[1].strip(" `") for match in re.finditer(r"^\|\s*([^|]+)\|", rows, re.MULTILINE)
                         if not isinstance(rules.style_family(match[1].strip(" `")), rules.NotStyleUnit))
        return StyleProduction(slug, branch[1].strip().removeprefix("refs/heads/"), declared or units)
    branches = git(ledger.repo, "for-each-ref", "--format=%(refname:short)", "--points-at", tip, "refs/heads").splitlines()
    branch_point = next((branch for branch in branches if not branch.startswith("style/")), tip[:7])
    return StyleProduction(slug, branch_point, units)


def previous_report(path: Path) -> FirstReport | PreviousReport:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return FirstReport()
    if not isinstance(value, dict):
        raise style_ledger.LedgerError(f"{path}: expected a report snapshot")
    data = cast(dict[str, object], value)
    old_rules = data.get("rules")
    files = data.get("files_by_day")
    proposals = data.get("proposals")
    if not isinstance(old_rules, dict) or not isinstance(files, dict) or not isinstance(proposals, list):
        raise style_ledger.LedgerError(f"{path}: invalid report snapshot")
    proposal_paths: set[str] = set()
    for proposal in cast(list[object], proposals):
        if not isinstance(proposal, str):
            raise style_ledger.LedgerError(f"{path}: invalid report snapshot")
        proposal_paths.add(proposal)
    progress: dict[str, RuleSnapshot] = {}
    for key, item in cast(dict[str, object], old_rules).items():
        if not isinstance(item, dict):
            raise style_ledger.LedgerError(f"{path}: invalid rule snapshot {key}")
        row = cast(dict[str, object], item)
        done, total = row.get("done"), row.get("cells_total")
        if not isinstance(done, bool) or type(total) is not int:
            raise style_ledger.LedgerError(f"{path}: invalid rule snapshot {key}")
        progress[key] = {"done": done, "cells_total": total}
    days: dict[str, int] = {}
    for day, count in cast(dict[str, object], files).items():
        if type(count) is not int:
            raise style_ledger.LedgerError(f"{path}: invalid file count")
        days[day] = count
    return PreviousReport(progress, frozenset(proposal_paths), days)


def save_snapshot(path: Path, snapshot: ReportSnapshot) -> None:
    descriptor, filename = tempfile.mkstemp(prefix=".last_report-", suffix=".json", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(snapshot, sort_keys=True) + "\n")
        _ = temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def one_decimal(value: float) -> str:
    return f"{value:.1f}".removesuffix(".0")


def percent(current: int, total: int) -> str:
    return one_decimal(100 * current / total) if total else "100"


def counted(count: int, noun: str) -> str:
    return f"{count:,} {noun}" + ("" if count == 1 else "s")


def table_row(name: str, progress: style_ledger.RuleCounts, counts: style_ledger.RuleCounts) -> str:
    current = (f"{counts.current:,} of {counts.total:,} {'crates' if progress.input_scope == 'manifest' else 'files'}" if progress.tier == "D"
               else f"{counts.current:,} ({percent(counts.current, counts.total)}%)")
    if counts.complete:
        done = f"yes, {counts.held:,} held" if counts.held else "yes"
    else:
        done = f"no: {counts.stale:,} stale" if counts.stale else "no"
    return (f"| {name} | {progress.tier} | {current} | {counts.stale:,} | {counts.open:,} | "
            + f"{counts.awaiting_merge:,} | {counts.held:,} | {done} |")


def resolve_rule(name: str, progress: tuple[style_ledger.RuleCounts, ...]) -> str:
    matches = [row.rule_id for row in progress if name in (row.rule_id, PurePosixPath(row.rule_id).stem)]
    if len(matches) != 1:
        detail = ": " + ", ".join(matches) if matches else ""
        raise style_ledger.LedgerError(f"{'ambiguous' if matches else 'unknown'} rule {name}{detail}")
    return matches[0]


def workspace(ledger: style_ledger.Ledger, tip: str) -> tuple[rules.WorkspaceMember, ...]:
    paths = git(ledger.repo, "ls-tree", "-rz", "--name-only", tip).split("\0")
    manifests = {path: git(ledger.repo, "show", f"{tip}:{path}") for path in paths if PurePosixPath(path).name == "Cargo.toml"}
    return rules.workspace_members(manifests)


def ticket_line(ticket: style_ledger.TicketRecord) -> str:
    at = datetime.fromisoformat(ticket["created_at"]).astimezone(user_zone())
    line = f"{ticket['id']} · {ticket['state']} · {ticket['unit']} · {ticket['crate']} · opened {at:%a %H:%M %Z}"
    return "\n".join([line, *(style_ledger.widened_line(widened) for widened in ticket["widened"]),
                      *(style_ledger.yielded_line(yielded) for yielded in ticket["yielded"]),
                      *(style_ledger.removed_line(removed) for removed in ticket["removed"])])


@dataclass(frozen=True)
class PreparedReport:
    text: str
    snapshot_path: Path
    snapshot: ReportSnapshot


def prepare_report(ledger: style_ledger.Ledger, rule: str = "", crate: str = "") -> PreparedReport:
    if rule and crate:
        raise style_ledger.LedgerError("choose --rule or --crate")
    repository_counts = ledger.rule_counts(style_ledger.WholeRepository())
    summary = ledger.summary_of(repository_counts)
    progress = tuple(row for row in repository_counts if row.tier in ("A", "B", "C", "D"))
    selected_rule = resolve_rule(rule, progress) if rule else ""
    members = workspace(ledger, summary.synced_tip)
    if crate:
        matches = [member.directory for member in members if crate in (member.directory, member.package_name)]
        if len(matches) != 1:
            raise style_ledger.LedgerError(f"unknown or ambiguous crate {crate}")
        crate = matches[0]
    counts = {row.rule_id: row for row in repository_counts}
    shown_counts = ({row.rule_id: row for row in ledger.rule_counts(style_ledger.CrateCoverage(crate))}
                    if crate else counts)
    style = production(ledger, summary.synced_tip)
    file_count = sum(len(ledger.rust_files(directory)) for directory in {".", *(member.directory for member in members)})
    snapshot_path = ledger.database.parent / "last_report.json"
    previous = previous_report(snapshot_path)
    now = datetime.now(user_zone())
    days = dict(previous.files_by_day) if isinstance(previous, PreviousReport) else {}
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    file_delta = f" ({file_count - days[yesterday]:+d} since yesterday)" if yesterday in days else ""
    synced = summary.synced_at.astimezone(user_zone())
    lines = [
        f"{style.slug} — synced {style.branch_point} {summary.synced_tip[:7]} at {synced:%H:%M %Z}, {file_count:,} files{file_delta}",
        f"Rules done: {summary.rules_done} of {summary.rules_total} ({sum(row.complete and row.tier == 'A' for row in progress)} by lint)"
        + f" · cells current {one_decimal(summary.cells_current_pct)}% · open {sum(row.open for row in counts.values()):,}"
        + f" · stale {summary.stale:,} · awaiting merge {sum(row.awaiting_merge for row in counts.values()):,}"
        + f" · held {sum(row.held for row in counts.values()):,}",
    ]
    unclassified = ledger.unclassified_rules()
    if unclassified:
        lines.append(f"Unclassified: {len(unclassified)} rules — pull nate_style")
    if isinstance(summary.waiting, style_ledger.PendingHandoffs):
        oldest = summary.waiting.oldest_pending_since.astimezone(user_zone())
        lines.append(f"Waiting to merge back: {counted(summary.waiting.count, 'checkpoint')}, oldest {oldest:%H:%M}")
    else:
        lines.append("Waiting to merge back: none")
    lines.extend(["", "| Rule | Tier | Current | Stale | Open | Awaiting merge | Held | Done |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"])
    stems = Counter(PurePosixPath(row.rule_id).stem for row in progress)
    for row in progress:
        if selected_rule and row.rule_id != selected_rule:
            continue
        stem = PurePosixPath(row.rule_id).stem
        lines.append(table_row(row.rule_id if stems[stem] > 1 else stem, row, shown_counts[row.rule_id]))
    if selected_rule or crate:
        tickets = sorted((ticket for unit in style.units for ticket in ledger.ticket_open(unit)
                          if (not selected_rule or selected_rule in {value.removesuffix("#uncovered")
                                                                   for value in (ticket["rule"], *ticket["rules"])})
                          and (not crate or ticket["crate"] == crate)), key=lambda ticket: (ticket["created_at"], ticket["id"]))
        if selected_rule:
            lines.extend(["", "Open tickets:", *(ticket_line(ticket) for ticket in tickets)] if tickets else ["", "Open tickets: none"])
        else:
            lines.extend(["", "Oldest open ticket: " + (ticket_line(tickets[0]) if tickets else "none")])
    gained = lost = lost_to_new_files = 0
    current_proposals = {str(finding.state.proposal) for finding in ledger.held_findings(style_ledger.HeldFindingsInState("filed"))
                         if isinstance(finding.state, style_ledger.Filed)}
    proposals = summary.proposals
    if isinstance(previous, PreviousReport):
        gained = sum(row.complete and not previous.rules.get(row.rule_id, {"done": False, "cells_total": 0})["done"] for row in progress)
        lost = sum(not row.complete and previous.rules.get(row.rule_id, {"done": False, "cells_total": 0})["done"] for row in progress)
        lost_to_new_files = sum(not row.complete and old["done"] and row.total > old["cells_total"]
                                for row in progress if (old := previous.rules.get(row.rule_id)) is not None)
        proposals = len(current_proposals - previous.proposals)
    cause = " to new files" if lost > 0 and lost == lost_to_new_files else ""
    lines.extend(["", f'Since last report: {counted(gained, "rule")} gained, {lost} lost "done"{cause}; '
                  + f'{counted(proposals, "proposal")} filed.'])
    days[now.date().isoformat()] = file_count
    snapshot: ReportSnapshot = {
        "rules": {row.rule_id: {"done": row.complete, "cells_total": row.total} for row in progress},
        "proposals": sorted(current_proposals), "files_by_day": dict(sorted(days.items())[-31:]),
    }
    return PreparedReport("\n".join(lines) + "\n", snapshot_path, snapshot)


def render_report(ledger: style_ledger.Ledger, rule: str = "", crate: str = "") -> str:
    return prepare_report(ledger, rule, crate).text


class ReportArguments(argparse.Namespace):
    repo: Path = Path()
    rule: str = ""
    crate: str = ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--repo", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    _ = selection.add_argument("--rule", default="")
    _ = selection.add_argument("--crate", default="")
    arguments = parser.parse_args(argv, namespace=ReportArguments())
    try:
        with style_ledger.open_ledger(arguments.repo) as ledger:
            report = prepare_report(ledger, arguments.rule, arguments.crate)
            print(report.text, end="", flush=True)
            save_snapshot(report.snapshot_path, report.snapshot)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"coverage_report: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
