#!/usr/bin/env python3
"""Select available style work and refill a unit's standing plan."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "lib"))
sys.path.insert(0, str(SCRIPTS / "production"))

import berth_board
from add_style import StandingStyleProduction, standing_style_production
import rules
import style_ledger
import style_footprint
import style_proposals
from issues import frontmatter
from production_units import Refusal, production_field
from unit_files import changed_files, git, unit_file_holders, unit_worktrees

FAMILIES: tuple[rules.Family, ...] = ("modules", "names", "values", "types")


@dataclass(frozen=True)
class AllCrates:
    pass


@dataclass(frozen=True)
class PilotCrates:
    members: frozenset[str]


PilotScope = AllCrates | PilotCrates
_ALL_CRATES = AllCrates()
_ALL_FAMILY_SCOPES: Mapping[rules.Family, PilotScope] = MappingProxyType(
    {family: _ALL_CRATES for family in FAMILIES})


def crate_in_scope(scope: PilotScope, crate: str) -> bool:
    return isinstance(scope, AllCrates) or crate in scope.members


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


def family_pilot_scopes(doc: Path, repo: Path) -> Mapping[rules.Family, PilotScope]:
    """Read each style unit's scope from its own standing plan copy."""
    scopes: dict[rules.Family, PilotScope] = {}
    for row in unit_worktrees(doc):
        for family in FAMILIES:
            if row.unit == family + "-unit":
                plan = row.worktree / "docs/style-production/units" / (row.unit + ".md")
                try:
                    text = plan.read_text()
                except FileNotFoundError:
                    scopes[family] = AllCrates()
                else:
                    scopes[family] = parse_pilot_scope(text, repo)
    return scopes


@dataclass(frozen=True)
class PhaseWritten:
    ticket_id: str


@dataclass(frozen=True)
class Resumed:
    ticket_id: str


@dataclass(frozen=True)
class RepairFirst:
    pass


@dataclass(frozen=True)
class WaitingForMerge:
    ticket_id: str
    checkpoint: str


@dataclass(frozen=True)
class WaitingForSync:
    unclassified: int


@dataclass(frozen=True)
class WaitingForStructure:
    crates: tuple[str, ...]
    unclassified: int


@dataclass(frozen=True)
class WaitingForBranchPointSync:
    branch_point: str
    branch_tip: str
    synced_tip: str


@dataclass(frozen=True)
class TicketSelection:
    kind: style_ledger.TicketKind
    family: rules.Family
    crate: str
    files: tuple[str, ...]
    rules: tuple[str, ...]
    rule: str = ""
    sites: tuple[style_ledger.ListedSite, ...] = ()
    cells: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class BusyFiles:
    board: berth_board.Board
    paths: frozenset[str]

    def contains(self, path: str) -> bool:
        return path in self.paths or any(reservation.blocks(path) for reservation in self.board.reservations())


NextPhase = PhaseWritten | Resumed | RepairFirst | WaitingForMerge | WaitingForSync | WaitingForStructure | WaitingForBranchPointSync | berth_board.BoardUnavailable


def style_document(repo: Path) -> Path:
    standing = standing_style_production(repo)
    if isinstance(standing, StandingStyleProduction):
        return standing.doc
    raise style_ledger.LedgerError(f"no standing style production: {standing.slug}")


def busy_files(repo: Path, doc: Path, ledger: style_ledger.Ledger, unit: str, board: berth_board.Board) -> BusyFiles:
    holders = unit_file_holders(repo, doc, ledger, unit)
    # The unit's existing open ticket is handled before selection, but its dirty paths still block recovery.
    busy = frozenset(path for paths in holders.values() for path in paths)
    return BusyFiles(board, busy)


def gate(repo: Path, ticket_id: str) -> int:
    try:
        with style_ledger.open_ledger(repo) as ledger:
            origin = ledger.ticket_origin(ticket_id)
            if isinstance(origin, style_ledger.SelectedByCoverage):
                missing = ledger.unjudged_ticket_cells(ticket_id)
                if missing:
                    for cell in missing:
                        print(f"unjudged: {cell.rule} {cell.path}")
                    return 1
                base = coverage_footprint_base(repo, ledger, origin.base)
                result = subprocess.run([sys.executable, str(SCRIPTS / "style/style_footprint.py"), "check",
                                         "--repo", str(repo), "--base", base, "--worktree"], check=False)
                return 0 if result.returncode == 0 else 1
            fields = frontmatter(origin.issue)
            findings = style_proposals.issue_findings(ledger, origin.issue)
            if (fields.get("status") != ["open"] or fields.get("unit") != [origin.unit] or not fields.get("plan")
                    or set(fields.get("style_findings", [])) != set(origin.finding_ids)
                    or any(not isinstance(finding.state, style_ledger.Filed) or finding.state.proposal != origin.issue
                           for finding in findings)):
                raise style_ledger.LedgerError("proposal approval or filed findings changed")
            allowed = {finding.path for finding in findings}
            outside = changed_files(repo) - allowed
            if outside:
                raise style_ledger.LedgerError("files outside approved findings: " + ", ".join(sorted(outside)))
            print("approved proposal")
            return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"review: {error}")
        return 1


def synced_tip(ledger: style_ledger.Ledger) -> str:
    with closing(sqlite3.connect(ledger.database)) as connection:
        rows = cast(list[tuple[str]], connection.execute("SELECT tip FROM sync WHERE id=1").fetchall())
    if not rows:
        raise style_ledger.LedgerError("no synced tip; run set-tip first")
    return rows[0][0]


def synced_style_branch(ledger: style_ledger.Ledger) -> str:
    with closing(sqlite3.connect(ledger.database)) as connection:
        rows = cast(list[tuple[str]], connection.execute("SELECT style_branch FROM sync WHERE id=1").fetchall())
    return rows[0][0] if rows else ""


def coverage_footprint_base(repo: Path, ledger: style_ledger.Ledger, recorded_base: str) -> str:
    """Exclude style-branch work taken in since the ticket's recorded start."""
    branch = synced_style_branch(ledger)
    if not branch:
        return recorded_base
    resolved = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"],
                              capture_output=True, text=True, check=False)
    if resolved.returncode == 1:
        return recorded_base
    if resolved.returncode:
        raise style_ledger.LedgerError(resolved.stderr.strip() or "git rev-parse failed")
    merged = subprocess.run(["git", "-C", str(repo), "merge-base", "HEAD", resolved.stdout.strip()],
                            capture_output=True, text=True, check=False)
    if merged.returncode == 1:
        return recorded_base
    if merged.returncode:
        raise style_ledger.LedgerError(merged.stderr.strip() or "git merge-base failed")
    base = merged.stdout.strip()
    ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", recorded_base, base],
                              capture_output=True, text=True, check=False)
    if ancestry.returncode == 1:
        return recorded_base
    if ancestry.returncode:
        raise style_ledger.LedgerError(ancestry.stderr.strip() or "git merge-base --is-ancestor failed")
    return base


@dataclass(frozen=True)
class BatchLimits:
    files: int
    lines: int
    floor: int


_READ_LIMITS = BatchLimits(25, 5000, 10)
_STRUCTURE_LIMITS = BatchLimits(6, 3000, 5)


def read_slices(repo: Path, tip: str, files: tuple[str, ...], limits: BatchLimits = _READ_LIMITS) -> tuple[tuple[str, ...], ...]:
    """Keep directories together, splitting only an oversized directory."""
    directories: dict[str, list[str]] = defaultdict(list)
    lengths = {path: len(git(repo, "show", f"{tip}:{path}").splitlines()) for path in files}
    for path in sorted(files):
        directories[str(PurePosixPath(path).parent)].append(path)
    groups: list[tuple[str, ...]] = []
    for directory in sorted(directories, key=lambda path: PurePosixPath(path).parts):
        paths = tuple(directories[directory])
        if len(paths) > limits.files or sum(lengths[path] for path in paths) > limits.lines:
            groups.extend((path,) for path in paths)
        else:
            groups.append(paths)
    return balanced_slices(tuple(groups), lengths, limits)


def balanced_slices(groups: tuple[tuple[str, ...], ...], lengths: dict[str, int],
                    limits: BatchLimits = _READ_LIMITS) -> tuple[tuple[str, ...], ...]:
    """Prefer feasible lower bounds before minimizing and balancing slice sizes."""
    costs = {len(groups): (0, 0, 0)}
    ends: dict[int, int] = {}
    for start in reversed(range(len(groups))):
        files, lines = 0, 0
        options: list[tuple[tuple[int, int, int], int]] = []
        for end in range(start + 1, len(groups) + 1):
            files += len(groups[end - 1])
            lines += sum(lengths[path] for path in groups[end - 1])
            if end > start + 1 and (files > limits.files or lines > limits.lines):
                break
            tail = costs[end]
            options.append(((tail[0] + int(files < limits.floor), tail[1] + 1, tail[2] + files * files), end))
        costs[start], ends[start] = min(options)
    result: list[tuple[str, ...]] = []
    start = 0
    while start < len(groups):
        end = ends[start]
        result.append(tuple(path for group in groups[start:end] for path in group))
        start = end
    return tuple(result)


def candidate_tickets(repo: Path, ledger: style_ledger.Ledger, busy: BusyFiles,
                      family: rules.Family, scope: PilotScope = _ALL_CRATES,
                      family_scopes: Mapping[rules.Family, PilotScope] = _ALL_FAMILY_SCOPES) -> tuple[TicketSelection, ...]:
    pending: list[style_ledger.Cell] = []
    stale: dict[str, int] = defaultdict(int)
    blocked: dict[str, int] = defaultdict(int)
    structure_crates: set[str] = set()
    for cell in ledger.cells():
        stale[cell.crate] += cell.state == "stale"
        if cell.state in ("open", "stale"):
            pending.append(cell)
            if cell.tier == "D":
                structure_crates.add(cell.crate)
            elif (cell.family is not None and cell.family != "modules" and cell.family in family_scopes
                  and crate_in_scope(family_scopes[cell.family], cell.crate)):
                blocked[cell.crate] += 1
    crates = sorted(stale, key=lambda crate: (-stale[crate], crate))
    if family == "modules":
        crates = sorted(structure_crates, key=lambda crate: (-blocked[crate], -stale[crate], crate)) + [
            crate for crate in crates if crate not in structure_crates
        ]
    tip = synced_tip(ledger)
    result: list[TicketSelection] = []
    for crate in crates:
        if not crate_in_scope(scope, crate):
            continue
        structure = tuple(cell for cell in pending if cell.crate == crate and cell.tier == "D")
        if structure:
            if family == "modules":
                manifest = str(PurePosixPath(crate) / "Cargo.toml")
                manifest_cells = tuple(cell for cell in structure if cell.path == crate)
                file_cells = tuple(cell for cell in structure if cell.path != crate and not busy.contains(cell.path))
                available = {cell.path for cell in file_cells}
                if manifest_cells and not busy.contains(manifest):
                    available.add(manifest)
                for files in sorted(read_slices(repo, tip, tuple(sorted(available)), _STRUCTURE_LIMITS)):
                    batch_cells = tuple(cell for cell in file_cells if cell.path in files)
                    if manifest in files:
                        batch_cells += manifest_cells
                    rule_ids = tuple(sorted({cell.rule for cell in batch_cells}))
                    sites = tuple(site for rule_id in rule_ids for site in ledger.listed_sites(rule_id, crate)
                                  if site.path in files)
                    result.append(TicketSelection("structure", family, crate, files, rule_ids, sites=sites,
                                                  cells=tuple(sorted((cell.rule, cell.path) for cell in batch_cells))))
            continue
        own = tuple(cell for cell in pending if cell.crate == crate and cell.family == family and not busy.contains(cell.path))
        for rule_id in sorted({cell.rule for cell in own if cell.tier == "B"}):
            files = {cell.path for cell in own if cell.rule == rule_id}
            sites_by_file: dict[str, list[style_ledger.ListedSite]] = defaultdict(list)
            for site in ledger.listed_sites(rule_id, crate):
                if site.path in files:
                    sites_by_file[site.path].append(site)
            packed: list[style_ledger.ListedSite] = []
            for path in sorted(sites_by_file):
                sites = sites_by_file[path]
                if packed and len(packed) + len(sites) > 150:
                    result.append(TicketSelection("listed", family, crate, tuple(dict.fromkeys(site.path for site in packed)),
                                                  (rule_id,), rule_id, tuple(packed)))
                    packed = []
                packed.extend(sites)
            if packed:
                result.append(TicketSelection("listed", family, crate, tuple(dict.fromkeys(site.path for site in packed)),
                                              (rule_id,), rule_id, tuple(packed)))
        read_rules: dict[str, set[str]] = defaultdict(set)
        for cell in own:
            if cell.tier == "C":
                read_rules[cell.path].add(cell.rule)
        by_rules: dict[tuple[str, ...], list[str]] = defaultdict(list)
        for path, rule_ids in sorted(read_rules.items()):
            by_rules[tuple(sorted(rule_ids))].append(path)
        for rule_ids, paths in sorted(by_rules.items()):
            for files in read_slices(repo, tip, tuple(paths)):
                result.append(TicketSelection("read", family, crate, files, rule_ids))
    return tuple(result)


def delegation_context(text: str) -> str:
    match = re.search(r"(?ms)^## Delegation Context\n(.*?)(?=^## |\Z)", text)
    if match is None:
        raise style_ledger.LedgerError("plan lacks Delegation Context")
    return match.group(1).strip()


@dataclass(frozen=True)
class ReadWindow:
    start: int
    end: int


_NO_READ_WINDOWS: Mapping[str, tuple[ReadWindow, ...]] = MappingProxyType({})


def read_windows(text: str) -> tuple[ReadWindow, ...]:
    """Balance line ranges, keeping a file's top-level test module apart."""
    lines = len(text.splitlines())
    cuts = [0, lines]
    for match in re.finditer(r"(?m)^[ \t]*#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]", text):
        prefix = style_footprint.tokens(text[:match.start()])
        depth = sum(token.value in ("{", "(", "[") for token in prefix) - sum(
            token.value in ("}", ")", "]") for token in prefix)
        if depth or any(token.kind == "unknown" for token in prefix):
            continue
        following = [token.value for token in style_footprint.tokens(text[match.end():])]
        cursor = 0
        while following[cursor:cursor + 2] == ["#", "["]:
            cursor += 2
            brackets = 1
            while cursor < len(following) and brackets:
                brackets += int(following[cursor] == "[") - int(following[cursor] == "]")
                cursor += 1
        if following[cursor:cursor + 1] == ["pub"]:
            cursor += 1
            if following[cursor:cursor + 1] == ["("]:
                cursor += 1
                parentheses = 1
                while cursor < len(following) and parentheses:
                    parentheses += int(following[cursor] == "(") - int(following[cursor] == ")")
                    cursor += 1
        if following[cursor:cursor + 1] != ["mod"]:
            continue
        cut = text.count("\n", 0, match.start())
        if cut:
            cuts.insert(1, cut)
        break
    windows: list[ReadWindow] = []
    for start, end in zip(cuts, cuts[1:]):
        length = end - start
        count = (length + 299) // 300
        if not count:
            continue
        size, extra = divmod(length, count)
        for index in range(count):
            finish = start + size + int(index < extra)
            windows.append(ReadWindow(start + 1, finish))
            start = finish
    return tuple(windows)


class CrateTargets(Enum):
    NO_PACKAGE = "no-package"
    LIBRARY = "library"
    BINARY_ONLY = "binary-only"


def phase_text(ticket: style_ledger.TicketRecord, texts: tuple[style_ledger.PinnedRuleText, ...], plan: Path,
               context: str, held_findings: tuple[style_ledger.HeldFinding, ...] = (),
               file_windows: Mapping[str, tuple[ReadWindow, ...]] = _NO_READ_WINDOWS,
               crate_targets: CrateTargets = CrateTargets.LIBRARY) -> str:
    ticket_id = ticket["id"]
    number = ticket_id.rsplit("-", 1)[1]
    rule_lines = [f"- `{text.rule_id}`: `{plan.parent.parent / 'rules' / (text.blob + '.md')}` (blob `{text.blob}`)" for text in texts]
    sites = [f"- {json.dumps(site, sort_keys=True, ensure_ascii=False)}" for site in ticket["sites"]]
    uncovered = [f"- `{pinned.rule_id}` uncovered: {match.group(1).strip()}"
                 for pinned in texts
                 if ticket["kind"] == "structure" or pinned.rule_id.endswith("#uncovered")
                 for match in re.finditer(r"(?m)^\s+uncovered:\s*(.+)$", pinned.text.decode())]
    workers = "2 writers; split files by subtree." if len(ticket["files"]) > 12 else (
        "1 writer + 1 tester; the tester opens as impl and re-reads the diff against the pinned rules."
    )
    per_file_structure = ticket["kind"] == "structure" and bool(ticket["cells"])
    manifest_rules = [rule for rule, path in ticket["cells"] if path == ticket["crate"]]
    owned_lines = (["Owned pending verdict cells (record only these rule/path pairs):"]
                   + [f"- `{rule}` · `{path}`" for rule, path in ticket["cells"]]) if per_file_structure else []
    held_lines = (["Already-held findings: do not hold these again; record held for a file whose finding still stands."]
                  + [f"- {finding.finding_id} · {finding.rule} · {finding.path}:{finding.line} · {finding.text}"
                     for finding in held_findings]) if per_file_structure and held_findings else []
    approved = ticket["kind"] == "proposal"
    files: list[str] = []
    for path in ticket["files"]:
        line = f"- `{path}`"
        if ticket["kind"] == "read":
            windows = file_windows[path]
            line += " — " + ("lines " + ", ".join(f"{window.start}-{window.end}" for window in windows)
                              if windows else "empty file")
        files.append(line)
    authorization = (
        f"Approved proposal `{ticket['proposal']}` overrides the mechanical-only invariant in the copied context "
        + "and the mechanical-only style Spec for exactly this issue's approved findings. Apply those changes; "
        + "Read the issue for its sites and before/after changes; hold every other non-mechanical finding. Package verification remains required."
        if approved else "Every structure finding is held as a proposal. Apply only mechanical findings from other tickets."
    )
    return "\n".join([
        f"### Phase {number} — {ticket_id}  · status: todo", "", "#### Work Order", "", context, "",
        "**Goal:**", (f"Judge these {len(ticket['files'])} files of `{ticket['crate']}` against every pinned rule below."
                      if per_file_structure else f"Judge the {ticket['kind']} ticket for {ticket['family']} in `{ticket['crate']}` against every pinned rule below."),
        "", "**Spec:**", *rule_lines, *sites, *uncovered, *owned_lines,
        authorization,
        "Read only these pinned rule texts. A #uncovered rule is the parent rule's uncovered read.",
        *([f"`{ticket['crate']}` is binary-only (no library target): none of its items, `pub` or not, is public outside the crate."]
          if crate_targets is CrateTargets.BINARY_ONLY else []),
        *([
            "Judge file by file: take each file under **Files** in turn and judge it against every rule above, with that rule's listed sites "
            + "in it and its uncovered surface; read the uncovered instructions in the pinned text.",
            "Before judging a file, read its sibling files in the same module and its direct callers "
            + "(search the crate for its types and functions), so placement and ownership are judged against the code around it.",
            "Hold every distinct defect as its own finding. Never fold several defects into one crate-level finding, "
            + "and never stop at a file's first finding.",
            "A defect that spans files is held once, under the file that should change. A defect several rules name is held once, "
            + "under the rule it breaks most directly, naming the others in its text.",
            ("Record each file's verdict for every rule above as soon as that file has been judged against it, "
             + "with `record --path <file>`; the gate refuses this ticket while any file lacks one."
             if per_file_structure else "Record each rule's crate verdict only after every file has been judged against it."),
            *[f"Record `{rule}` once with `--path {ticket['crate']}` after reading the manifests." for rule in manifest_rules],
            *held_lines,
        ] if ticket["kind"] == "structure" else []),
        *([
            "Judge window by window: take the files under **Files** in order, and each file's windows in order.",
            "For each window, read exactly that line range in full and judge it against every rule above, "
            + "line by line, giving its last line the same attention as its first.",
            "Start a file's first window with its module docs (the `//!` block and any comment before the first item); "
            + "judge them against every rule before any code.",
            "Hold that window's findings before opening the next window. Hold each distinct defect as its own finding; "
            + "never stop at a window's first finding.",
            "Record a file's verdict for every rule only after its last window has been judged, "
            + "with `record --rule <rule> --path <file>` for each rule/file cell.",
        ] if ticket["kind"] == "read" else []),
        "Product work wins. Skip reserved, unmerged or dirty files; follow the yielding procedure in ~/.claude/docs/style_production.md.",
        ("Apply the approved findings. Hold other findings: " if approved else "Apply only mechanical fixes under ~/.claude/docs/style_production.md. Hold every other finding: ")
        + f"`style_ledger.py held add --ticket {ticket_id}` with the pinned rule blob, rule, site path, line and text, kind, before and after; "
        + "record that cell with `record --verdict held`.",
        f"Record every judged cell with `style_ledger.py record --ticket {ticket_id}`; the ledger takes its pinned rule blob.",
        "", "**Files:**", *files, "", "**Workers:**", workers, "",
        "**Acceptance gate:**", "For each touched package run:",
        "`bash ~/.claude/scripts/delegate/verify.sh check <package>`",
        "`bash ~/.claude/scripts/delegate/verify.sh test <package>`",
        "`bash ~/.claude/scripts/delegate/verify.sh lint <package>`",
        f"`~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py gate --repo . --ticket {ticket_id}`",
        "Every ticket cell has a recorded verdict.", "",
    ])


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            _ = temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def write_phase(ledger: style_ledger.Ledger, ticket: style_ledger.TicketRecord, plan: Path, text: str) -> None:
    texts = ledger.pinned_rule_texts(ticket["id"])
    ticket["base"] = ledger.ticket_base(ticket["id"])
    crate = PurePosixPath(ticket["crate"])
    manifest: dict[str, object] = tomllib.loads(git(ledger.repo, "show", f"{ticket['base']}:{crate / 'Cargo.toml'}"))
    if not isinstance(manifest.get("package"), dict):
        crate_targets = CrateTargets.NO_PACKAGE
    elif isinstance(manifest.get("lib"), dict) or git(
            ledger.repo, "ls-tree", "--name-only", ticket["base"], "--", str(crate / "src/lib.rs")).strip():
        crate_targets = CrateTargets.LIBRARY
    else:
        crate_targets = CrateTargets.BINARY_ONLY
    file_windows = ({path: read_windows(git(ledger.repo, "show", f"{ticket['base']}:{path}"))
                     for path in ticket["files"]} if ticket["kind"] == "read" else {})
    held = (tuple(finding for finding in ledger.held_findings(style_ledger.AllHeldFindings())
                  if finding.path in ticket["files"] and finding.rule in ticket["rules"]
                  and not isinstance(finding.state, (style_ledger.Waived, style_ledger.Built)))
            if ticket["kind"] == "structure" and ticket["cells"] else ())
    phase = phase_text(ticket, texts, plan, delegation_context(text), held, file_windows, crate_targets)
    for pinned in texts:
        path = plan.parent.parent / "rules" / (pinned.blob + ".md")
        if not path.exists() or git(plan.parent, "hash-object", str(path)).strip() != pinned.blob:
            atomic_write(path, pinned.text)
    if any(match.group("id") == ticket["id"] for match in PHASE.finditer(text)):
        updated = PHASE.sub(lambda match: phase if match.group("id") == ticket["id"] else match.group(0), text)
        if updated != text:
            atomic_write(plan, updated.encode())
        return
    heading = re.search(r"^## Phases\s*$", text, re.MULTILINE)
    if heading is None:
        raise style_ledger.LedgerError("plan lacks Phases heading")
    following = re.search(r"^## ", text[heading.end():], re.MULTILINE)
    end = heading.end() + following.start() if following else len(text)
    atomic_write(plan, (text[:end].rstrip() + "\n\n" + phase + text[end:]).encode())


PHASE = re.compile(r"(?ms)^### Phase \d+ — (?P<id>[^\s]+)[^\n]*(?:\n|\Z).*?(?=^### Phase |^## |\Z)")


def discard_finished(ledger: style_ledger.Ledger, text: str) -> tuple[str, tuple[str, ...]]:
    with closing(sqlite3.connect(ledger.database)) as connection:
        states = dict(cast(list[tuple[str, str]], connection.execute("SELECT id,state FROM tickets").fetchall()))
    changes: list[str] = []
    def keep(match: re.Match[str]) -> str:
        ticket_id = match.group("id")
        state = states.get(ticket_id, "")
        if state in ("merged", "dropped"):
            changes.append(f"discarded {ticket_id}: {state}")
            return ""
        return match.group(0)
    return PHASE.sub(keep, text), tuple(changes)


@dataclass(frozen=True)
class NoAvailableWork:
    pass


def _select_phase(
    ledger: style_ledger.Ledger, unit: str, branch: str, plan: Path, repo: Path,
    doc: Path, text: str, scope: PilotScope, busy: BusyFiles,
) -> PhaseWritten | NoAvailableWork | WaitingForStructure:
    queued = ledger.queued_tickets(unit)
    for index, ticket in enumerate(queued):
        in_scope = crate_in_scope(scope, ticket["crate"])
        if index == 0 and in_scope and not any(busy.contains(path) for path in ticket["files"]):
            ledger.ticket_assign(ticket["id"], branch)
            for extra in queued[1:]:
                ledger.ticket_drop(extra["id"])
                print(f"dropped {extra['id']}: files busy")
            origin = ledger.ticket_origin(ticket["id"])
            if isinstance(origin, style_ledger.ApprovedProposal):
                style_proposals.proposal_command("start", str(origin.issue))
            write_phase(ledger, ledger.ticket_open(unit)[0], plan, text)
            return PhaseWritten(ticket["id"])
        ledger.ticket_drop(ticket["id"])
        reason = "files busy" if in_scope else "outside pilot scope"
        print(f"dropped {ticket['id']}: {reason}")
    family = unit.removesuffix("-unit")
    if family not in FAMILIES:
        raise style_ledger.LedgerError(f"{unit}: unknown style family")
    try:
        folder = Path(production_field(doc.read_text().splitlines(), "Proposals")).expanduser()
    except Refusal:
        issue = style_proposals.NoApprovedProposal()
    else:
        if not folder.is_absolute():
            folder = doc.parent.parent / folder
        issue = style_proposals.approved_issue(folder, unit, ledger)
    if not isinstance(issue, style_proposals.NoApprovedProposal):
        findings = style_proposals.issue_findings(ledger, issue.path)
        if any(not isinstance(finding.state, style_ledger.Filed) or finding.state.proposal != issue.path for finding in findings):
            raise style_ledger.LedgerError(f"{issue.path}: every approved finding must be filed")
        if any(not crate_in_scope(scope, style_proposals.crate_for(ledger, finding.path)) for finding in findings):
            print(f"waiting {issue.path}: outside pilot scope")
        elif any(busy.contains(finding.path) for finding in findings):
            print(f"waiting {issue.path}: files busy")
        else:
            ticket_id = ledger.next_ticket_id(unit)
            ledger.ticket_add(ticket_id, unit, "proposal", family, style_proposals.crate_for(ledger, findings[0].path),
                              sorted({finding.path for finding in findings}), sorted({finding.rule for finding in findings}),
                              proposal=str(issue.path))
            ledger.ticket_assign(ticket_id, branch)
            style_proposals.proposal_command("start", str(issue.path))
            write_phase(ledger, ledger.ticket_open(unit)[0], plan, text)
            return PhaseWritten(ticket_id)
    candidates = candidate_tickets(repo, ledger, busy, family, scope, family_pilot_scopes(doc, repo))
    if not candidates:
        if family != "modules":
            pending = tuple(cell for cell in ledger.cells()
                            if cell.state in ("open", "stale") and crate_in_scope(scope, cell.crate))
            structure = {cell.crate for cell in pending if cell.tier == "D"}
            own = {cell.crate for cell in pending if cell.family == family}
            blocked = tuple(sorted(structure & own))
            if blocked:
                return WaitingForStructure(blocked, len(ledger.unclassified_rules()))
        return NoAvailableWork()
    selected = candidates[0]
    ticket_id = ledger.next_ticket_id(unit)
    ledger.ticket_add(ticket_id, unit, selected.kind, selected.family, selected.crate, list(selected.files),
                      list(selected.rules), selected.rule, [dict(rule=site.rule, path=site.path, line=site.line, text=site.text) for site in selected.sites],
                      cells=selected.cells)
    ledger.ticket_assign(ticket_id, branch)
    write_phase(ledger, ledger.ticket_open(unit)[0], plan, text)
    return PhaseWritten(ticket_id)


def next_phase(unit: str, plan: Path, repo: Path) -> NextPhase:
    plan, repo = plan.expanduser().resolve(), repo.expanduser().resolve()
    doc = style_document(repo)
    row = next((row for row in unit_worktrees(doc) if row.unit == unit), None)
    if row is None:
        raise style_ledger.LedgerError(f"{unit}: no Units row in {doc}")
    expected = row.worktree.resolve() / "docs/style-production/units" / (unit + ".md")
    if plan != expected:
        raise style_ledger.LedgerError(f"--plan must be the unit worktree's own copy: {expected}")
    original = plan.read_text()
    scope = parse_pilot_scope(original, repo)
    if any("repair" in line.lower() and re.search(r"status:\s*todo", line) for line in original.splitlines() if line.startswith("### Phase ")):
        return RepairFirst()
    with style_ledger.open_ledger(repo) as ledger:
        style_proposals.complete(ledger)
        text, changes = discard_finished(ledger, original)
        if text != original:
            atomic_write(plan, text.encode())
        for change in changes:
            print(change)
        opened = ledger.ticket_open(unit)
        if opened:
            ticket = opened[0]
            if ticket["state"] == "checkpointed":
                return WaitingForMerge(ticket["id"], ticket["checkpoint"])
            origin = ledger.ticket_origin(ticket["id"])
            if isinstance(origin, style_ledger.ApprovedProposal):
                style_proposals.proposal_command("start", str(origin.issue))
            write_phase(ledger, ticket, plan, text)
            ledger.clear_sync_wait(unit)
            return Resumed(ticket["id"])
        tip = synced_tip(ledger)
        standing = standing_style_production(repo)
        if not isinstance(standing, StandingStyleProduction):
            raise style_ledger.LedgerError(f"no standing style production: {standing.slug}")
        branch_tip = git(repo, "rev-parse", "--verify", f"{standing.branch_point}^{{commit}}").strip()
        ancestry = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", branch_tip, tip],
            capture_output=True, text=True, check=False,
        )
        if ancestry.returncode == 1:
            ledger.wait_for_sync(unit)
            return WaitingForBranchPointSync(standing.branch_point, branch_tip, tip)
        if ancestry.returncode:
            raise style_ledger.LedgerError(ancestry.stderr.strip() or "git merge-base --is-ancestor failed")
        board = berth_board.read_board(repo)
        if isinstance(board, berth_board.BoardUnavailable):
            return board
        busy = busy_files(repo, doc, ledger, unit, board)
        selected = _select_phase(ledger, unit, row.branch, plan, repo, doc, text, scope, busy)
        if isinstance(selected, PhaseWritten):
            ledger.clear_sync_wait(unit)
            return selected
        ledger.wait_for_sync(unit)
        board = berth_board.read_board(repo)
        if isinstance(board, berth_board.BoardUnavailable):
            return board
        busy = busy_files(repo, doc, ledger, unit, board)
        selected = _select_phase(ledger, unit, row.branch, plan, repo, doc, text, scope, busy)
        if isinstance(selected, PhaseWritten):
            ledger.clear_sync_wait(unit)
            return selected
        if isinstance(selected, WaitingForStructure):
            return selected
        return WaitingForSync(len(ledger.unclassified_rules()))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    refill = sub.add_parser("next-phase")
    _ = refill.add_argument("--unit", required=True)
    _ = refill.add_argument("--plan", type=Path, required=True)
    _ = refill.add_argument("--repo", type=Path, required=True)
    ticket_gate = sub.add_parser("gate")
    _ = ticket_gate.add_argument("--repo", type=Path, required=True)
    _ = ticket_gate.add_argument("--ticket", required=True)
    queue = sub.add_parser("queue")
    _ = queue.add_argument("--repo", type=Path, required=True)
    _ = queue.add_argument("--unit")
    args = cast(PlanArguments, parser.parse_args(argv))
    try:
        if args.command == "gate":
            return gate(args.repo, args.ticket)
        if args.command == "queue":
            board = berth_board.read_board(args.repo)
            if isinstance(board, berth_board.BoardUnavailable):
                print(f"board unavailable: {board.reason}")
                return 4
            with style_ledger.open_ledger(args.repo) as ledger:
                doc = style_document(args.repo)
                busy = busy_files(args.repo, doc, ledger, args.unit or "", board)
                scopes = family_pilot_scopes(doc, args.repo)
                for family in FAMILIES:
                    unit = family + "-unit"
                    if args.unit and args.unit != unit:
                        continue
                    for ticket in candidate_tickets(args.repo, ledger, busy, family,
                                                    scopes.get(family, _ALL_CRATES), scopes):
                        print(f"{unit}: {ticket.kind} {ticket.crate} {ticket.rule or ','.join(ticket.rules)} — {len(ticket.files)} files: {', '.join(ticket.files)}")
            return 0
        result = next_phase(args.unit, args.plan, args.repo)
        match result:
            case PhaseWritten(ticket_id):
                print(f"phase written: {ticket_id}")
            case Resumed(ticket_id):
                print(f"resume {ticket_id}")
            case RepairFirst():
                print("repair first")
            case WaitingForMerge(ticket_id, checkpoint):
                print(f"waiting for merge: {ticket_id} checkpointed at {checkpoint[:7]}")
                return 5
            case berth_board.BoardUnavailable(reason):
                print(f"board unavailable: {reason}")
                return 4
            case WaitingForSync(unclassified):
                print("waiting for sync")
                if unclassified:
                    print(f"unclassified: {unclassified} rules — pull nate_style", file=sys.stderr)
                return 3
            case WaitingForStructure(crates, unclassified):
                print(f"waiting for structure: {', '.join(crates)}")
                if unclassified:
                    print(f"unclassified: {unclassified} rules — pull nate_style", file=sys.stderr)
                return 3
            case WaitingForBranchPointSync(branch_point, branch_tip, tip):
                print(f"waiting for sync: {branch_point} {branch_tip[:7]} is not in the synced tip {tip[:7]}")
                return 3
        return 0
    except (OSError, ValueError, Refusal, sqlite3.Error) as error:
        print(f"style_plan: {error}", file=sys.stderr)
        return 2


class PlanArguments(argparse.Namespace):
    command: str = ""
    ticket: str = ""
    repo: Path = Path()
    unit: str = ""
    plan: Path = Path()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
