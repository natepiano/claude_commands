#!/usr/bin/env python3
"""Merge, test, push, promote, and record one production checkpoint."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, cast

from add_unit import Production, Refusal, cell_value, read_production, unit_rows


class ValidateAndPush(NamedTuple):
    pass


class GitPush(NamedTuple):
    pass


class PushedTip(NamedTuple):
    tip: str


class NotYetPushed(NamedTuple):
    pass


class FieldPresent(NamedTuple):
    text: str


class FieldAbsent(NamedTuple):
    pass


class NoPromotion(NamedTuple):
    pass


class NoMac(NamedTuple):
    pass


class MacCheckout(NamedTuple):
    path: Path


class PromoteTo(NamedTuple):
    checkouts: tuple[Path, ...]
    mac: NoMac | MacCheckout


class PromotionAlreadyAtTip(NamedTuple):
    tip: str


class PromotionAdvanced(NamedTuple):
    tip: str


class PromotionOriginPushed(NamedTuple):
    tip: str


class PromotionNotPushed(NamedTuple):
    tip: str
    already_at_tip: bool


class FirstMerge(NamedTuple):
    pass


class LastMerged(NamedTuple):
    hash: str


class NoMerge(NamedTuple):
    pass


class MergeEntry(NamedTuple):
    unit: str
    phase: str
    short: str
    merge_branch: str
    merge_hash: str
    kind: CodeCheckpoint | ShrinkCommit


class MergeBranchHistory(NamedTuple):
    entries: tuple[MergeEntry, ...]

    def last_merge(self) -> MergeEntry | NoMerge:
        return self.entries[0] if self.entries else NoMerge()

    def last_for_unit(self, unit: str) -> MergeEntry | NoMerge:
        return next((entry for entry in self.entries if entry.unit == unit), NoMerge())

    def last_code_for_unit(self, unit: str) -> MergeEntry | NoMerge:
        return next((entry for entry in self.entries
                     if entry.unit == unit and isinstance(entry.kind, CodeCheckpoint)), NoMerge())

    def code_merge_count(self) -> int:
        return sum(isinstance(entry.kind, CodeCheckpoint) for entry in self.entries)

    def has_code_merge(self, unit: str, phase: str) -> bool:
        return any(entry.unit == unit and entry.phase == phase and isinstance(entry.kind, CodeCheckpoint)
                   for entry in self.entries)


class CodeCheckpoint(NamedTuple):
    pass


class ShrinkCommit(NamedTuple):
    pass


class ReviewTrial(NamedTuple):
    ux_findings: str
    code_findings: str
    review_minutes: str
    ux_check_minutes: str
    ux_repair_minutes: str


class NoReviewTrial(NamedTuple):
    pass


class Unit(NamedTuple):
    name: str
    plan: Path
    worktree: Path
    branch: str
    owns: tuple[str, ...]


class MergeRequest(NamedTuple):
    production: Production
    unit: Unit
    phase: str
    commit: str
    also: tuple[str, ...]
    kind: CodeCheckpoint | ShrinkCommit
    push: ValidateAndPush | GitPush
    promotion: NoPromotion | PromoteTo
    merge_tests: tuple[str, ...]
    flakes: frozenset[str]
    units: tuple[Unit, ...]
    lines: tuple[str, ...]
    scratch: Path
    trailer: tuple[str, ...]
    review_trial: ReviewTrial | NoReviewTrial
    delivers: str
    cancel_prior: bool
    started: str
    regime: str
    holds: int
    merge_defects: int
    excluded: str


class Stop(Exception):
    def __init__(self, step: str, state: str, detail: str, message: str) -> None:
        super().__init__(detail)
        self.step: str = step
        self.state: str = state
        self.detail: str = detail
        self.message: str = message


def report(step: str, state: str, detail: str) -> None:
    print(f"{step}: {state} — {detail}", flush=True)


def git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False)


def good(result: subprocess.CompletedProcess[str], step: str) -> str:
    if result.returncode:
        raise Stop(step, "failed", result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}",
                   "the showrunner will inspect the failed command")
    return result.stdout.strip()


def optional_field(lines: list[str], field: str) -> FieldPresent | FieldAbsent:
    prefix = f"- **{field}:**"
    for line in lines:
        if line.startswith(prefix):
            return FieldPresent(line[len(prefix):].removeprefix(" "))
    return FieldAbsent()


def promotion_from_doc(lines: list[str]) -> NoPromotion | PromoteTo:
    """Read the production's promotion destinations in their declared order."""
    field = optional_field(lines, "Promote")
    if isinstance(field, FieldAbsent):
        return NoPromotion()
    promote_raw = field.text.split(" — ", 1)[0].replace("`", "")
    local: list[Path] = []
    mac: NoMac | MacCheckout = NoMac()
    for item in promote_raw.split(","):
        item = item.strip()
        if item.startswith("mac "):
            mac = MacCheckout(Path(item[4:].strip()).expanduser())
        elif item:
            local.append(Path(item).expanduser())
    return PromoteTo(tuple(local), mac) if local or isinstance(mac, MacCheckout) else NoPromotion()


def parse_units(lines: list[str]) -> tuple[Unit, ...]:
    _, rows = unit_rows(lines)
    result: list[Unit] = []
    for row in rows:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        plan = Path(cell_value(cells[1]).split(" ", 1)[0])
        owns_cell = cells[6]
        quoted = cast(list[str], re.findall(r"`([^`]+)`", owns_cell))
        plain = owns_cell.split(" — ", 1)[0]
        if quoted:
            owned: list[str] = []
            sibling_directory = ""
            for path in quoted:
                owned.append(path)
                if "/" in path:
                    sibling_directory = path.rstrip("/") if path.endswith("/") else str(Path(path).parent)
                elif sibling_directory:
                    # A bare name is a root file (`settings.json`) or a sibling of the path before it.
                    owned.append((Path(sibling_directory) / path).as_posix())
            owns = tuple(owned)
        else:
            owns = tuple(part.strip() for part in plain.split(",") if part.strip())
        result.append(Unit(cell_value(cells[0]), plan,
                           Path(cell_value(cells[2])).expanduser(), cell_value(cells[3]), owns))
    return tuple(result)


def parse_merge_tests(lines: list[str]) -> tuple[str, ...]:
    field = optional_field(lines, "Merge tests")
    if isinstance(field, FieldAbsent):
        raise Refusal("production doc lacks Merge tests")
    first_comment = len(field.text)
    in_backticks = False
    for index, char in enumerate(field.text):
        if char == "`":
            in_backticks = not in_backticks
        elif char == "(" and not in_backticks:
            first_comment = index
            break
    return tuple(re.findall(r"`([^`]+)`", field.text[:first_comment]))


def request_from(args: argparse.Namespace) -> MergeRequest:
    production = read_production(Path(cast(str, args.production)))
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    units = parse_units(lines)
    name = cast(str, args.unit)
    unit = next((candidate for candidate in units if candidate.name == name), None)
    if unit is None:
        raise Refusal(f"unit {name} not in production doc")
    push_field = optional_field(lines, "Push")
    push_raw = cell_value(push_field.text) if isinstance(push_field, FieldPresent) else "validate_and_push"
    if push_raw not in ("git", "validate_and_push"):
        raise Refusal(f"unknown Push mode: {push_raw}")
    push: ValidateAndPush | GitPush = GitPush() if push_raw == "git" else ValidateAndPush()
    if cast(bool, args.cancel_prior) and isinstance(push, GitPush):
        raise Refusal("--cancel-prior applies only to Push: validate_and_push")
    promotion = promotion_from_doc(lines)
    flakes_field = optional_field(lines, "Known flakes")
    flakes: frozenset[str] = frozenset()
    if isinstance(flakes_field, FieldPresent):
        quoted = re.findall(r"`([^`]+)`", flakes_field.text)
        names = quoted if quoted else flakes_field.text.split(" — ", 1)[0].split(",")
        flakes = frozenset(name.strip() for name in names if name.strip())
    kind: CodeCheckpoint | ShrinkCommit = ShrinkCommit() if cast(bool, args.shrink) else CodeCheckpoint()
    trial = cast(str | None, args.review_trial)
    if isinstance(kind, CodeCheckpoint) and not trial:
        raise Refusal("code checkpoint requires --review-trial")
    review_trial: ReviewTrial | NoReviewTrial = review_numbers(trial) if trial else NoReviewTrial()
    delivers = cast(str | None, args.delivers)
    if isinstance(kind, CodeCheckpoint) and (not delivers or not delivers.strip()):
        raise Refusal("code checkpoint requires --delivers")
    if delivers and ("\n" in delivers or "\r" in delivers):
        raise Refusal("--delivers must be one line")
    if isinstance(kind, ShrinkCommit) and delivers:
        raise Refusal("--delivers applies only to a code checkpoint")
    scratch = Path(cast(str | None, args.scratch) or os.environ.get("MERGE_CHECKPOINT_SCRATCH")
                   or tempfile.gettempdir()).expanduser()
    return MergeRequest(production, unit, cast(str, args.phase), cast(str, args.hash),
                        tuple(cast(list[str], args.also)), kind, push, promotion,
                        parse_merge_tests(lines), flakes, units, tuple(lines), scratch,
                        tuple(cast(list[str], args.trailer)), review_trial, delivers or "",
                        cast(bool, args.cancel_prior), cast(str, args.started),
                        cast(str, args.regime), cast(int, args.holds), cast(int, args.merge_defects),
                        cast(str | None, args.excluded) or "")


def merge_branch_history(checkout: Path, merge_branch: str) -> MergeBranchHistory:
    """Read checkpoint merge subjects on a branch, newest first."""
    output = good(git(checkout, "log", "--first-parent", merge_branch, "--format=%H%x00%s"), "ancestry")
    pattern = re.compile(r"^Merge (.+?) phase (\S+?)( shrink)? \(([0-9a-f]+)\)")
    entries: list[MergeEntry] = []
    for row in output.splitlines():
        merge_hash, separator, subject = row.partition("\x00")
        match = pattern.match(subject) if separator else None
        if match is None:
            continue
        unit, phase, shrink, short = match.groups()
        entries.append(MergeEntry(unit, phase, short, merge_branch, merge_hash,
                                  ShrinkCommit() if shrink else CodeCheckpoint()))
    return MergeBranchHistory(tuple(entries))


def merge_history(request: MergeRequest) -> FirstMerge | LastMerged:
    latest = merge_branch_history(request.production.checkout, request.production.merge_branch).last_code_for_unit(
        request.unit.name)
    if isinstance(latest, NoMerge):
        return FirstMerge()
    checkpoint = good(git(request.production.checkout, "rev-parse", latest.short + "^{commit}"), "ancestry")
    return LastMerged(checkpoint)


def code_merged(request: MergeRequest) -> bool:
    history = merge_branch_history(request.production.checkout, request.production.merge_branch)
    return history.has_code_merge(request.unit.name, request.phase)


def ancestry(request: MergeRequest) -> bool:
    checkout = request.production.checkout
    if git(checkout, "cat-file", "-e", f"{request.commit}^{{commit}}").returncode:
        raise Stop("ancestry", "failed", f"commit {request.commit} is absent",
                   "push the checkpoint and send its hash again")
    history = merge_history(request)
    if isinstance(history, LastMerged) and git(checkout, "merge-base", "--is-ancestor", history.hash,
                                               request.commit).returncode:
        # A rerun of a previously merged hash is valid even when later checkpoints landed.
        if git(checkout, "merge-base", "--is-ancestor", request.commit, request.production.merge_branch).returncode:
            raise Stop("ancestry", "held", "checkpoint drops an earlier merge",
                       "merge the latest unit checkpoint and send a new hash")
    _ = good(git(checkout, "fetch", "origin", request.unit.branch), "ancestry")
    if git(checkout, "merge-base", "--is-ancestor", request.commit, f"origin/{request.unit.branch}").returncode:
        raise Stop("ancestry", "held", "commit is not on origin",
                   "push your branch to origin and resend the checkpoint")
    already = git(checkout, "merge-base", "--is-ancestor", request.commit,
                  request.production.merge_branch).returncode == 0
    if isinstance(request.kind, ShrinkCommit) and not code_merged(request):
        raise Stop("ancestry", "held", f"phase {request.phase} code checkpoint has not merged",
                   "wait for the phase code checkpoint to merge, then resend the shrink")
    report("ancestry", "ok", "checkpoint exists, follows history, and is on origin" +
           ("; already merged" if already else ""))
    return already


def changed_paths(request: MergeRequest) -> tuple[str, ...]:
    output = good(git(request.production.checkout, "diff", "--name-only",
                      f"{request.production.merge_branch}...{request.commit}"), "scope")
    return tuple(line for line in output.splitlines() if line)


def allowed(path: str, ownership: tuple[str, ...]) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") for item in ownership)


def scope(request: MergeRequest, paths: tuple[str, ...]) -> None:
    staged = good(git(request.production.checkout, "diff", "--cached", "--name-only"), "scope")
    if staged:
        raise Stop("scope", "held", f"staged paths in merge checkout: {staged.replace(chr(10), ', ')}",
                   "the showrunner will clear unrelated staged paths, then rerun the checkpoint")
    if isinstance(request.kind, ShrinkCommit):
        plan = request.unit.plan.as_posix()
        next_items = str(Path(plan).with_name(Path(plan).stem + "-next.md"))
        unexpected = [path for path in paths if path not in (plan, next_items)]
        if unexpected or plan not in paths:
            raise Stop("scope", "held", f"shrink paths must be {plan} and optional {next_items}; got {', '.join(paths)}",
                       "commit only the plan doc and its next-items file, then resend the shrink")
    else:
        hubs = hub_paths(request.lines, request.unit.name)
        permitted = (*request.unit.owns, *request.also, *hubs)
        unexpected = [path for path in paths if not allowed(path, permitted)]
        if unexpected:
            raise Stop("scope", "held", f"outside Owns: {', '.join(unexpected)}",
                       f"explain why these paths changed: {', '.join(unexpected)}")
    report("scope", "ok", f"{len(paths)} changed paths are allowed")


def hub_paths(lines: tuple[str, ...], unit: str) -> tuple[str, ...]:
    if "## Hub files" not in lines:
        return ()
    start = lines.index("## Hub files") + 1
    result: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if not line.startswith("|") or line.startswith("| ---") or line.startswith("| File"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 3 and unit in (cells[1] + " " + cells[2]):
            result.append(cell_value(cells[0]))
    return tuple(result)


def conflicts(request: MergeRequest) -> None:
    result = git(request.production.checkout, "merge-tree", "--write-tree", "--name-only",
                 request.production.merge_branch, request.commit)
    if result.returncode:
        if result.returncode == 1:
            raise Stop("conflicts", "held", "conflict: " + (result.stdout.strip() or result.stderr.strip()),
                       "merge the merge branch on your unit branch, resolve conflicts, and send a new hash")
        raise Stop("conflicts", "failed", result.stderr.strip() or f"merge-tree exit {result.returncode}",
                   "the showrunner will inspect the merge-tree failure")
    report("conflicts", "ok", "merge tree is clean")


def other_units(request: MergeRequest, paths: tuple[str, ...]) -> None:
    for unit in request.units:
        if unit.name == request.unit.name:
            continue
        diff = git(request.production.checkout, "diff", "--name-only",
                   f"{request.production.merge_branch}...{unit.branch}")
        branch_paths: set[str] = set(diff.stdout.splitlines()) if diff.returncode == 0 else set()
        status = git(unit.worktree, "status", "--short")
        worktree_paths = {line[3:] for line in status.stdout.splitlines() if len(line) >= 4}
        overlap = sorted(set(paths) & (branch_paths | worktree_paths))
        report("other units", "ok", f"{unit.name}: {', '.join(overlap) if overlap else 'no overlap'}")
    if len(request.units) == 1:
        report("other units", "ok", "no other units")


def gate_names(request: MergeRequest, first_code_merge: bool) -> str:
    if not first_code_merge or "## Gates" not in request.lines:
        return "no gate"
    start = request.lines.index("## Gates") + 1
    found: list[str] = []
    for line in request.lines[start:]:
        if line.startswith("## "):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[2].lower() == f"{request.unit.name} phase {request.phase}".lower():
            found.append(f"{cells[0]}: {cells[1]}")
    return ", ".join(found) if found else "no gate"


def into(path: Path, branch: str, why: str, unblocks: str) -> None:
    print(f"into: {path} ({branch}) — {why}; unblocks: {unblocks}", flush=True)


def merge_subject(request: MergeRequest) -> str:
    kind = " shrink" if isinstance(request.kind, ShrinkCommit) else ""
    return (f"Merge {request.unit.name} phase {request.phase}{kind} "
            f"({request.commit[:7]}) into {request.production.merge_branch}")


def merged_checkpoint(request: MergeRequest) -> str:
    output = good(git(request.production.checkout, "log", request.production.merge_branch,
                      "--format=%H%x00%s"), "merge")
    subject = merge_subject(request)
    for row in output.splitlines():
        head, _, actual = row.partition("\x00")
        if actual == subject:
            return head
    raise Stop("merge", "failed", f"merge subject absent: {subject}",
               "the showrunner will inspect the merge branch")


def merge(request: MergeRequest) -> str:
    checkout = request.production.checkout
    staged = good(git(checkout, "diff", "--cached", "--name-only"), "merge")
    if staged:
        raise Stop("merge", "held", f"staged paths in merge checkout: {staged.replace(chr(10), ', ')}",
                   "the showrunner will clear unrelated staged paths, then rerun the checkpoint")
    short = request.commit[:7]
    request.scratch.mkdir(parents=True, exist_ok=True)
    message_path = request.scratch / f"merge_{short}.msg"
    message = merge_subject(request) + "\n\n" + ("Phase plan shrink." if isinstance(request.kind, ShrinkCommit)
                                                  else request.delivers)
    if request.trailer:
        message += "\n\n" + "\n".join(request.trailer)
    _ = message_path.write_text(message + "\n", encoding="utf-8")
    result = git(checkout, "merge", "--no-ff", "-q", "-F", str(message_path), request.commit)
    if result.returncode:
        raise Stop("merge", "failed", result.stderr.strip() or "merge failed",
                   "the showrunner will inspect the merge failure")
    head = good(git(checkout, "rev-parse", "HEAD"), "merge")
    report("merge", "ok", f"merged as {head}")
    return head


def tool(name: str, fallback: Path, override: str) -> str:
    return os.environ.get(override) or shutil.which(name) or str(fallback)


def test_steps(request: MergeRequest, paths: tuple[str, ...]) -> tuple[list[tuple[str, list[str]]], Path]:
    checkout = request.production.checkout
    packages: set[str] = set()
    examples: set[tuple[str, str]] = set()
    for raw in paths:
        path = checkout / raw
        for parent in (path.parent, *path.parents):
            if parent == checkout.parent:
                break
            if (parent / "Cargo.toml").exists():
                packages.add(parent.name)
                break
        match = re.search(r"(?:^|/)crates/([^/]+)/examples/([^/.]+)", raw)
        if match:
            examples.add((match.group(1), match.group(2)))
    verify = tool("verify.sh", Path.home() / ".claude/scripts/delegate/verify.sh", "MERGE_CHECKPOINT_VERIFY")
    verify_command = [verify] if os.access(verify, os.X_OK) else ["bash", verify]
    steps = [(f"package {package}", [*verify_command, "test", package]) for package in sorted(packages)]
    steps.extend((f"example {package}/{name}", [*verify_command, "example", package, name])
                 for package, name in sorted(examples))
    steps.extend((f"merge test {index}", shlex.split(command))
                 for index, command in enumerate(request.merge_tests, 1))
    return steps, request.scratch / f"merge_{request.commit[:7]}_test.log"


def run_test_command(command: list[str], checkout: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=checkout, capture_output=True, text=True, check=False)
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, "", f"{error}\n")


def undo_unpushed_merge(request: MergeRequest, merge_head: str) -> None:
    checkout = request.production.checkout
    head = good(git(checkout, "rev-parse", "HEAD"), "red")
    if head != merge_head:
        raise Stop("red", "failed", f"HEAD moved from unpushed merge {merge_head}",
                   "the showrunner will inspect the merge branch")
    if git(checkout, "merge-base", "--is-ancestor", head,
           f"origin/{request.production.merge_branch}").returncode == 0:
        raise Stop("red", "failed", "merge is already pushed; cannot reset it",
                   "the showrunner will inspect the merge branch")
    _ = good(git(checkout, "reset", "--keep", "HEAD~1"), "red")


def tests(request: MergeRequest, paths: tuple[str, ...], merge_head: str) -> str:
    if isinstance(request.kind, ShrinkCommit):
        report("test", "ok", "shrink has no package tests")
        return "no tests"
    steps, log = test_steps(request, paths)
    failed: list[tuple[str, list[str]]] = []
    with log.open("w", encoding="utf-8") as output:
        for name, command in steps:
            result = run_test_command(command, request.production.checkout)
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{name.replace(' ', '_')}_EXIT={result.returncode}\n")
            output.flush()
            if not test_passed(command, result):
                failed.append((name, command))
    report("test", "ok", f"{len(steps)} commands ran; {len(failed)} red; log {log}")
    unresolved: list[str] = []
    for name, command in failed:
        with log.open("a", encoding="utf-8") as output:
            result = run_test_command(command, request.production.checkout)
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{name.replace(' ', '_')}_RERUN_EXIT={result.returncode}\n")
        package = name.removeprefix("package ")
        if test_passed(command, result) and package in request.flakes:
            note = f"known flake {package}: red in batch, green alone"
            request.production.log.parent.mkdir(parents=True, exist_ok=True)
            with request.production.log.open("a", encoding="utf-8") as ledger:
                _ = ledger.write(f"- {datetime.now(request.production.zone):%H:%M %Z}: {note}\n")
        else:
            unresolved.append(name)
    if unresolved:
        undo_unpushed_merge(request, merge_head)
        raise Stop("red", "failed", f"{', '.join(unresolved)} failed; log {log}",
                   f"repair {', '.join(unresolved)} and send a new checkpoint; test log: {log}")
    report("red", "ok", "all commands green or known flakes green alone")
    return ", ".join(name for name, _ in steps) if steps else "no tests"


def test_passed(command: list[str], result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return True
    if Path(command[0]).name != "basedpyright":
        return False
    lines = (result.stdout + result.stderr).strip().splitlines()
    return bool(lines and re.fullmatch(r"0 errors, 0 warnings(?:, 0 notes)?", lines[-1].strip()))


def push_git(request: MergeRequest, may_undo: bool) -> str:
    checkout = request.production.checkout
    old_origin = good(git(checkout, "rev-parse", f"origin/{request.production.merge_branch}"), "push")
    try:
        _ = good(git(checkout, "fetch", "origin", "main"), "push")
        main = git(checkout, "rev-parse", "origin/main")
        if main.returncode == 0 and git(checkout, "merge-base", "--is-ancestor", "origin/main", "HEAD").returncode:
            _ = good(git(checkout, "merge", "--no-ff", "-m",
                         "Merge origin/main into " + request.production.merge_branch, "origin/main"), "push")
        tip = good(git(checkout, "rev-parse", "HEAD"), "push")
        _ = good(git(checkout, "push", "origin", request.production.merge_branch), "push")
    except Stop as error:
        if may_undo:
            if git(checkout, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
                _ = good(git(checkout, "merge", "--abort"), "push")
            remote = good(git(checkout, "ls-remote", "origin", f"refs/heads/{request.production.merge_branch}"),
                          "push").split()[0]
            if remote == old_origin:
                _ = good(git(checkout, "reset", "--keep", old_origin), "push")
        raise error
    report("push", "ok", f"{request.production.merge_branch} pushed at {tip}")
    return tip


def pushed_state(request: MergeRequest) -> PushedTip | NotYetPushed:
    checkout = request.production.checkout
    branch = request.production.merge_branch
    _ = good(git(checkout, "fetch", "origin", branch), "push")
    remote = f"origin/{branch}"
    if git(checkout, "merge-base", "--is-ancestor", request.commit, remote).returncode:
        return NotYetPushed()
    return PushedTip(good(git(checkout, "rev-parse", remote), "push"))


def push_validate(request: MergeRequest, may_undo: bool) -> str:
    script = tool("validate_and_push.sh", Path.home() / ".claude/scripts/validate_and_push/validate_and_push.sh",
                  "MERGE_CHECKPOINT_VALIDATE_AND_PUSH")
    command = [script, "--quick", "--to", request.production.merge_branch,
               "--fix-commit", f"ci({request.production.slug}): format fixes after {request.unit.name} phase {request.phase}"]
    if request.cancel_prior:
        command.append("--cancel-prior")
    result = subprocess.run(command,
                            cwd=request.production.checkout, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        if may_undo:
            reset = git(request.production.checkout, "reset", "--keep", f"origin/{request.production.merge_branch}")
            if reset.returncode:
                detail += f"; reset failed: {reset.stderr.strip()}"
        raise Stop("push", "failed", detail, "repair the push failure and send a new checkpoint")
    tip = good(git(request.production.checkout, "rev-parse", "HEAD"), "push")
    report("push", "ok", f"validated and pushed {tip}")
    return tip


def promote_local_checkout(checkout: Path, merge_branch: str, tip: str) -> (
        PromotionAlreadyAtTip | PromotionOriginPushed | PromotionAdvanced | PromotionNotPushed):
    """Fast-forward one main checkout and push any eligible unpushed tip."""
    _ = good(git(checkout, "fetch", "origin", merge_branch), "promote")
    current_branch = good(git(checkout, "branch", "--show-current"), "promote")
    if current_branch != "main":
        raise Stop("promote", "failed", f"{checkout} is on {current_branch}, expected main",
                   "switch the promotion checkout to main and rerun this checkpoint")
    current = good(git(checkout, "rev-parse", "main"), "promote")
    _ = good(git(checkout, "fetch", "origin", "main"), "promote")
    if current == tip:
        unpushed = good(git(checkout, "rev-list", "origin/main..main"), "promote")
        foreign = good(git(checkout, "rev-list", "main", "^origin/main", f"^origin/{merge_branch}"),
                       "promote")
        if foreign:
            return PromotionNotPushed(tip, True)
        if unpushed:
            _ = good(git(checkout, "push", "origin", "main"), "promote")
            return PromotionOriginPushed(tip)
        return PromotionAlreadyAtTip(tip)
    result = git(checkout, "merge", "--ff-only", tip)
    if result.returncode:
        status = git(checkout, "status", "--short").stdout.strip()
        raise Stop("promote", "failed", f"{checkout}: {status or result.stderr.strip()}",
                   "the showrunner will clear the listed checkout paths and rerun this checkpoint")
    unpushed = good(git(checkout, "rev-list", "origin/main..main"), "promote")
    foreign = good(git(checkout, "rev-list", "main", "^origin/main", f"^origin/{merge_branch}"), "promote")
    if foreign:
        return PromotionNotPushed(tip, False)
    if unpushed:
        _ = good(git(checkout, "push", "origin", "main"), "promote")
    return PromotionAdvanced(tip)


def pull_mac_checkout(mac: Path, source: Path) -> None:
    remote = (f"cd {shlex.quote(str(mac))} && git pull --ff-only "
              f"natedev:{shlex.quote(str(source))} main; printf 'rc=%s\\n' \"$?\"")
    result = subprocess.run(["ssh", "mac", remote], capture_output=True, text=True, check=False)
    match = re.search(r"(?:^|\n)rc=(\d+)", result.stdout)
    if result.returncode or match is None or int(match.group(1)):
        raise Stop("promote", "failed", f"mac {mac}: {result.stdout.strip()} {result.stderr.strip()}",
                   "the showrunner will repair the Mac pull and rerun this checkpoint")


def promote(request: MergeRequest, tip: str) -> None:
    if isinstance(request.promotion, NoPromotion):
        report("promote", "ok", "no promotion configured")
        return
    for checkout in request.promotion.checkouts:
        result = promote_local_checkout(checkout, request.production.merge_branch, tip)
        already_at = isinstance(result, (PromotionAlreadyAtTip, PromotionOriginPushed)) or (
            isinstance(result, PromotionNotPushed) and result.already_at_tip)
        detail = f"{checkout} {'already at' if already_at else 'main at'} {tip}"
        if isinstance(result, PromotionNotPushed):
            detail += "; main holds commits not on origin/" + request.production.merge_branch + " and was not pushed"
        report("promote", "ok", detail)
        into(checkout, "main", f"sessions on {os.uname().nodename} run the installed commands from it",
             f"the live gate of {request.unit.name} phase {request.phase}")
    if isinstance(request.promotion.mac, MacCheckout):
        mac = request.promotion.mac.path
        source = request.promotion.checkouts[0] if request.promotion.checkouts else request.production.checkout
        pull_mac_checkout(mac, source)
        report("promote", "ok", f"mac {mac} main pulled")
        into(mac, "main", "sessions on mac run the installed commands from it",
             f"the live gate of {request.unit.name} phase {request.phase}")


def review_numbers(line: str) -> ReviewTrial:
    pattern = (r"review trial: ux (\d+) findings, code (\d+) findings, review-seat minutes ([\d.]+), "
               r"ux check minutes ([\d.]+), ux repair minutes ([\d.]+)")
    match = re.fullmatch(pattern, line.strip())
    if match is None:
        raise Refusal("--review-trial must read: review trial: ux <N> findings, code <N> findings, "
                      + "review-seat minutes <M>, ux check minutes <U>, ux repair minutes <R>")
    return ReviewTrial(*match.groups())


def record(request: MergeRequest, merge_head: str, tests_green: str) -> None:
    recorded = (f"{request.unit.name} phase {request.phase} ({request.commit}) merged as {merge_head}; ")
    if request.production.log.exists() and recorded in request.production.log.read_text(encoding="utf-8"):
        report("record", "ok", "checkpoint already recorded")
        return
    if isinstance(request.kind, CodeCheckpoint):
        numbers = request.review_trial
        if isinstance(numbers, NoReviewTrial):
            raise Stop("record", "failed", "review trial absent", "send the notice's full review trial line")
        merged = good(git(request.production.checkout, "show", "-s", "--format=%cI", merge_head), "record")
        script = tool("review_regime.py", Path.home() / ".claude/scripts/production/review_regime.py",
                      "MERGE_CHECKPOINT_REVIEW_REGIME")
        review_command = [script] if os.access(script, os.X_OK) else [sys.executable, script]
        command = [*review_command, "add", "--unit", request.unit.name, "--phase", request.phase,
                   "--regime", request.regime, "--started", request.started, "--merged", merged,
                   "--holds", str(request.holds), "--merge-defects", str(request.merge_defects),
                   "--ux-findings", numbers.ux_findings, "--code-findings", numbers.code_findings,
                   "--review-minutes", numbers.review_minutes, "--ux-check-minutes", numbers.ux_check_minutes,
                   "--ux-repair-minutes", numbers.ux_repair_minutes]
        if request.excluded:
            command.extend(("--excluded", request.excluded))
        result = subprocess.run(command, cwd=request.production.checkout, capture_output=True, text=True, check=False)
        if result.returncode:
            raise Stop("record", "failed", result.stderr.strip() or result.stdout.strip(),
                       "the showrunner will repair the review record and rerun")
    request.production.log.parent.mkdir(parents=True, exist_ok=True)
    with request.production.log.open("a", encoding="utf-8") as log:
        suffix = "; promoted" if isinstance(request.promotion, PromoteTo) else ""
        _ = log.write(f"- {datetime.now(request.production.zone):%H:%M %Z}: {request.unit.name} phase {request.phase} "
                      + f"({request.commit}) merged as {merge_head}; {tests_green} green; pushed{suffix}\n")
    report("record", "ok", "review ledger and production log updated" if isinstance(request.kind, CodeCheckpoint)
           else "shrink logged without review-ledger row")


def run(request: MergeRequest) -> int:
    try:
        already = ancestry(request)
        paths: tuple[str, ...] = ()
        tests_green = "no tests" if isinstance(request.kind, ShrinkCommit) else "merge tests"
        if not already:
            paths = changed_paths(request)
            scope(request, paths)
            conflicts(request)
            other_units(request, paths)
            merge_head = merge(request)
            try:
                tests_green = tests(request, paths, merge_head)
            except (OSError, ValueError) as error:
                undo_unpushed_merge(request, merge_head)
                raise Stop("red", "failed", str(error),
                           "repair the test logging failure and send a new checkpoint") from error
        else:
            merge_head = merged_checkpoint(request)
            for step in ("scope", "conflicts", "other units", "merge", "test", "red"):
                report(step, "ok", "checkpoint already merged; resuming promotion")
        first_code_merge = not already and isinstance(request.kind, CodeCheckpoint)
        pushed = pushed_state(request) if already else NotYetPushed()
        if isinstance(pushed, PushedTip):
            tip = pushed.tip
            report("push", "ok", f"{request.production.merge_branch} already pushed at {tip}")
        elif isinstance(request.push, GitPush):
            tip = push_git(request, not already)
        else:
            tip = push_validate(request, not already)
        into(request.production.checkout, request.production.merge_branch,
             "the merge branch collects every unit's checkpoints", gate_names(request, first_code_merge))
        promote(request, tip)
        record(request, merge_head, tests_green)
        print(f"send {request.unit.name}: phase {request.phase} merged and pushed as {merge_head}; continue with the next step")
        return 0
    except Stop as error:
        report(error.step, error.state, error.detail.replace("\n", "; "))
        print(f"send {request.unit.name}: {error.message}")
        return 1
    except (OSError, ValueError) as error:
        report("run", "failed", str(error).replace("\n", "; "))
        print(f"send {request.unit.name}: the showrunner will inspect the failed command")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("unit")
    _ = parser.add_argument("phase")
    _ = parser.add_argument("hash")
    _ = parser.add_argument("--also", action="append", default=[])
    _ = parser.add_argument("--shrink", action="store_true")
    _ = parser.add_argument("--trailer", action="append", default=[])
    _ = parser.add_argument("--review-trial")
    _ = parser.add_argument("--delivers")
    _ = parser.add_argument("--cancel-prior", action="store_true")
    _ = parser.add_argument("--started", required=True)
    _ = parser.add_argument("--regime", choices=("after", "trial"), default="after")
    _ = parser.add_argument("--holds", type=int, default=0)
    _ = parser.add_argument("--merge-defects", type=int, default=0)
    _ = parser.add_argument("--excluded")
    _ = parser.add_argument("--scratch")
    args = parser.parse_args()
    try:
        return run(request_from(args))
    except (Refusal, OSError, ValueError) as error:
        report("input", "failed", str(error))
        print(f"send {cast(str, args.unit)}: correct the checkpoint input and resend it")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
