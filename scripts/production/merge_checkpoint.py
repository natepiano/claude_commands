#!/usr/bin/env python3
"""Merge, test, push, promote, and record one production checkpoint.

A unit checkpoint commit without its Change-Review line is held before the merge. After the push,
each visible change becomes a row in the change-review list of the production's top-level showrunner
(`change_review.py`), with the doc stamped into its frontmatter; the first row creates a missing list, and the list commits itself when it sits in a git
work tree. A refused commit warns and never fails the merge. A recorded code checkpoint ends its
phase: the scratch folders of task `phase:<unit>/<phase>` (scripts/lib/scratch.py) are removed.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePath, PurePosixPath
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import change_review
import scratch  # on the path once change_review has put scripts/lib there
from scripts.production.showrunners import canonical_slug
from production_units import Production, read_production
from production_units import Refusal, cell_value, production_field, unit_table

from scripts.direct import progress_history  # noqa: E402
from scripts.production import production_doc  # noqa: E402
from scripts.lib.probe_session import PROBE_ENV  # noqa: E402

# /zone queues a change for the Mac while it is offline; a promotion's Mac pull sends it.
ZONE_SCRIPT = Path(__file__).resolve().parents[1] / "lib/zone.py"
TREE_CHANGED_STATUS = 77


class ValidateAndPush(NamedTuple):
    pass


class GitPush(NamedTuple):
    pass


class PushedTip(NamedTuple):
    tip: str


class NotYetPushed(NamedTuple):
    pass


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
    kind: CodeCheckpoint | ShrinkCommit | AsBuiltCommit


class ChildHandoffEntry(NamedTuple):
    child: str
    short: str
    merge_branch: str
    merge_hash: str


class MergeBranchHistory(NamedTuple):
    entries: tuple[MergeEntry | ChildHandoffEntry, ...]

    def last_merge(self) -> MergeEntry | ChildHandoffEntry | NoMerge:
        return self.entries[0] if self.entries else NoMerge()

    def last_for_unit(self, unit: str) -> MergeEntry | NoMerge:
        return next((entry for entry in self.entries if isinstance(entry, MergeEntry) and entry.unit == unit), NoMerge())

    def last_code_for_unit(self, unit: str) -> MergeEntry | NoMerge:
        return next((entry for entry in self.entries
                     if isinstance(entry, MergeEntry) and entry.unit == unit and isinstance(entry.kind, CodeCheckpoint)), NoMerge())

    def code_merge_count(self) -> int:
        return sum(isinstance(entry, MergeEntry) and isinstance(entry.kind, CodeCheckpoint) for entry in self.entries)

    def has_code_merge(self, unit: str, phase: str) -> bool:
        return any(isinstance(entry, MergeEntry) and entry.unit == unit and entry.phase == phase and isinstance(entry.kind, CodeCheckpoint)
                   for entry in self.entries)

    def last_handoff_for_child(self, child: str) -> ChildHandoffEntry | NoMerge:
        return next((entry for entry in self.entries
                     if isinstance(entry, ChildHandoffEntry) and entry.child == child), NoMerge())


class CodeCheckpoint(NamedTuple):
    pass


class ShrinkCommit(NamedTuple):
    pass


class AsBuiltCommit(NamedTuple):
    """A unit's closing commit: its as-built docs and the plan's deletion, after its last phase merged."""


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
    kind: CodeCheckpoint | ShrinkCommit | AsBuiltCommit
    push: ValidateAndPush | GitPush
    promotion: production_doc.NoPromotion | production_doc.PromoteTo
    merge_tests: tuple[tuple[str, ...], ...]
    flakes: frozenset[str]
    flaky_tests: frozenset[str]
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


@contextmanager
def merge_lock(checkout: Path, slug: str) -> Generator[None]:
    """Held for a whole merge gate. A unit rename takes it before it writes the merge branch, so
    neither lands a commit inside the other's run."""
    common = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        raise OSError(f"{checkout}: no git directory for the merge lock: {common.stderr.strip()}")
    with (Path(common.stdout.strip()) / f"showrunner-merge-{slug}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"merge lock: waiting — a merge gate or a unit rename is writing {slug}'s merge branch", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False)


def good(result: subprocess.CompletedProcess[str], step: str) -> str:
    if result.returncode:
        raise Stop(step, "failed", result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}",
                   "the showrunner will inspect the failed command")
    return result.stdout.strip()


def listed_names(lines: list[str], field: str) -> frozenset[str]:
    """A doc field's names: the backtick-quoted ones, else a comma list before " — "."""
    present = production_doc.optional_field(lines, field)
    if isinstance(present, production_doc.FieldAbsent):
        return frozenset()
    quoted = re.findall(r"`([^`]+)`", present.text)
    names = quoted if quoted else present.text.split(" — ", 1)[0].split(",")
    return frozenset(name.strip() for name in names if name.strip())


def parse_units(lines: list[str]) -> tuple[Unit, ...]:
    result: list[Unit] = []
    for cells in unit_table(lines):
        if not all(heading in cells for heading in ("Unit", "Plan", "Worktree", "Branch", "Owns")):
            continue
        plan = Path(cell_value(cells["Plan"]).split(" ", 1)[0])
        owns_cell = cells["Owns"]
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
        result.append(Unit(cell_value(cells["Unit"]), plan,
                           Path(cell_value(cells["Worktree"])).expanduser(), cell_value(cells["Branch"]), owns))
    return tuple(result)


def parse_merge_tests(lines: list[str], checkout: Path) -> tuple[tuple[str, ...], ...]:
    """Each command the Merge tests line names, split and expanded, checked before any merge starts.

    The commands run with no shell, so `~` and `$VAR` are expanded here. A command must start with a
    program on PATH or an executable file in the showrunner checkout: a package name or a directory
    written there by mistake is refused by name.
    """
    field = production_doc.optional_field(lines, "Merge tests")
    if isinstance(field, production_doc.FieldAbsent):
        raise Refusal("production doc lacks Merge tests")
    first_comment = len(field.text)
    in_backticks = False
    for index, char in enumerate(field.text):
        if char == "`":
            in_backticks = not in_backticks
        elif char == "(" and not in_backticks:
            first_comment = index
            break
    commands: list[tuple[str, ...]] = []
    for entry in cast(list[str], re.findall(r"`([^`]+)`", field.text[:first_comment])):
        try:
            words = [os.path.expandvars(os.path.expanduser(word)) for word in shlex.split(entry)]
        except ValueError as error:
            raise Refusal(f"Merge tests entry `{entry}` does not split into words: {error}") from error
        if not words:
            raise Refusal(f"Merge tests entry `{entry}` is empty")
        for word in words:
            unset = re.search(r"\$\{?([A-Za-z_]\w*)", word)
            if unset:
                raise Refusal(f"Merge tests entry `{entry}` names ${unset[1]}, which is not set")
        program = words[0]
        located = checkout / program if "/" in program else shutil.which(program)
        if located is None or not (Path(located).is_file() and os.access(located, os.X_OK)):
            raise Refusal(f"Merge tests entry `{entry}` is not a command: {program} is neither on PATH "
                          + f"nor an executable file in {checkout}")
        commands.append(tuple(words))
    return tuple(commands)


def request_from(args: argparse.Namespace) -> MergeRequest:
    production = read_production(Path(cast(str, args.production)))
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    units = parse_units(lines)
    name = cast(str, args.unit)
    unit = next((candidate for candidate in units if candidate.name == name), None)
    if unit is None:
        raise Refusal(f"unit {name} not in production doc")
    push_field = production_doc.optional_field(lines, "Push")
    push_raw = cell_value(push_field.text) if isinstance(push_field, production_doc.FieldPresent) else "validate_and_push"
    if push_raw not in ("git", "validate_and_push"):
        raise Refusal(f"unknown Push mode: {push_raw}")
    push: ValidateAndPush | GitPush = GitPush() if push_raw == "git" else ValidateAndPush()
    if cast(bool, args.cancel_prior) and isinstance(push, GitPush):
        raise Refusal("--cancel-prior applies only to Push: validate_and_push")
    promotion = production_doc.promotion_from_doc(lines)
    # Checked at load, so a bad destination stops the checkpoint before anything is pushed.
    if isinstance(promotion, production_doc.PromoteTo):
        for checkout in promotion.checkouts:
            if not checkout.is_dir():
                raise Refusal(f"Promote checkout {checkout} does not exist; name an existing checkout, or none")
    flakes = listed_names(lines, "Known flakes")
    flaky_tests = listed_names(lines, "Known flaky tests")
    if cast(bool, args.shrink) and cast(bool, args.as_built):
        raise Refusal("--shrink and --as-built are separate checkpoints")
    kind: CodeCheckpoint | ShrinkCommit | AsBuiltCommit = (
        ShrinkCommit() if cast(bool, args.shrink) else AsBuiltCommit() if cast(bool, args.as_built) else CodeCheckpoint())
    trial = cast(str | None, args.review_trial)
    if isinstance(kind, CodeCheckpoint) and not trial:
        raise Refusal("code checkpoint requires --review-trial")
    review_trial: ReviewTrial | NoReviewTrial = review_numbers(trial) if trial else NoReviewTrial()
    if isinstance(kind, CodeCheckpoint):
        check_parallel(cast(str | None, args.parallel))
    delivers = cast(str | None, args.delivers)
    if isinstance(kind, CodeCheckpoint) and (not delivers or not delivers.strip()):
        raise Refusal("code checkpoint requires --delivers")
    if delivers and ("\n" in delivers or "\r" in delivers):
        raise Refusal("--delivers must be one line")
    if not isinstance(kind, CodeCheckpoint) and delivers:
        raise Refusal("--delivers applies only to a code checkpoint")
    scratch = Path(cast(str | None, args.scratch) or os.environ.get("MERGE_CHECKPOINT_SCRATCH")
                   or tempfile.gettempdir()).expanduser()
    return MergeRequest(production, unit, cast(str, args.phase), cast(str, args.hash),
                        tuple(cast(list[str], args.also)), kind, push, promotion,
                        parse_merge_tests(lines, production.checkout), flakes, flaky_tests, units, tuple(lines), scratch,
                        tuple(cast(list[str], args.trailer)), review_trial, delivers or "",
                        cast(bool, args.cancel_prior), cast(str, args.started),
                        cast(str, args.regime), cast(int, args.holds), cast(int, args.merge_defects),
                        cast(str | None, args.excluded) or "")


def merge_branch_history(checkout: Path, merge_branch: str) -> MergeBranchHistory:
    """Read checkpoint merge subjects on a branch, newest first.

    A `chore(<slug>): rename unit <old> to <new>` subject carries a unit's history over: every
    older entry under `<old>` counts as `<new>`, through a chain of renames, while a later unit
    that reuses `<old>` keeps its own newer entries. The commit is the record; no alias is stored.
    """
    output = good(git(checkout, "log", "--first-parent", merge_branch, "--format=%H%x00%s"), "ancestry")
    pattern = re.compile(r"^Merge (.+?) phase (\S+?)( shrink| as-built)? \(([0-9a-f]+)\)")
    handoff = re.compile(r"^Merge (.+?) hand-off \(([0-9a-f]+)\)")
    rename = re.compile(r"^chore\([^)]*\): rename unit (\S+) to (\S+)$")
    renamed: dict[str, str] = {}
    entries: list[MergeEntry | ChildHandoffEntry] = []
    for row in output.splitlines():
        merge_hash, separator, subject = row.partition("\x00")
        moved = rename.match(subject) if separator else None
        if moved is not None:
            old, new = moved.groups()
            renamed[old] = renamed.get(new, new)
            continue
        child = handoff.match(subject) if separator else None
        if child is not None:
            entries.append(ChildHandoffEntry(child[1], child[2], merge_branch, merge_hash))
            continue
        match = pattern.match(subject) if separator else None
        if match is None:
            continue
        unit, phase, docs, short = match.groups()
        unit = renamed.get(unit, unit)
        kind = ShrinkCommit() if docs == " shrink" else AsBuiltCommit() if docs == " as-built" else CodeCheckpoint()
        entries.append(MergeEntry(unit, phase, short, merge_branch, merge_hash, kind))
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


def on_origin(request: MergeRequest) -> str | None:
    """Find an origin branch holding the checkpoint, trying the Units row first."""
    checkout = request.production.checkout
    fetched = git(checkout, "fetch", "origin", request.unit.branch)
    # A missing Units-row branch is not a failure: another head may hold this phase.
    missing = fetched.returncode and git(checkout, "ls-remote", "--exit-code", "origin",
                                        f"refs/heads/{request.unit.branch}").returncode == 2
    if not missing:
        _ = good(fetched, "ancestry")
        if git(checkout, "merge-base", "--is-ancestor", request.commit, "FETCH_HEAD").returncode == 0:
            return f"origin/{request.unit.branch}"
    # Explicit heads also work with a narrow fetch refspec. Prune so a deleted
    # remote branch cannot certify a checkpoint through its stale tracking ref.
    _ = good(git(checkout, "fetch", "--prune", "origin",
                 "+refs/heads/*:refs/remotes/origin/*"), "ancestry")
    refs = good(git(checkout, "for-each-ref", "--contains", request.commit,
                    "--format=%(refname) %(symref)", "refs/remotes/origin/"), "ancestry")
    for line in refs.splitlines():
        fields = line.split()
        if len(fields) == 1:  # origin/HEAD is an alias, not an origin branch.
            return fields[0].removeprefix("refs/remotes/")
    return None


def ancestry(request: MergeRequest) -> bool:
    checkout = request.production.checkout
    history = merge_history(request)
    if isinstance(history, LastMerged) and git(checkout, "merge-base", "--is-ancestor", history.hash,
                                               request.commit).returncode:
        # A rerun of a previously merged hash is valid even when later checkpoints landed.
        if git(checkout, "merge-base", "--is-ancestor", request.commit, request.production.merge_branch).returncode:
            raise Stop("ancestry", "held", "checkpoint drops an earlier merge",
                       "merge the latest unit checkpoint and send a new hash")
    origin_branch = on_origin(request)
    if origin_branch is None:
        raise Stop("ancestry", "held", "commit is not on origin",
                   "push your branch to origin and resend the checkpoint")
    already = git(checkout, "merge-base", "--is-ancestor", request.commit,
                  request.production.merge_branch).returncode == 0
    if not isinstance(request.kind, CodeCheckpoint) and not code_merged(request):
        raise Stop("ancestry", "held", f"phase {request.phase} code checkpoint has not merged",
                   "wait for the phase code checkpoint to merge, then resend the shrink")
    report("ancestry", "ok", f"checkpoint exists, follows history, and is on {origin_branch}" +
           ("; already merged" if already else ""))
    return already


def change_review_commits(request: MergeRequest) -> list[change_review.Commit]:
    """The unit's own commits in this checkpoint; hold it when one lacks its Change-Review line."""
    history = merge_history(request)
    previous = history.hash if isinstance(history, LastMerged) else f"origin/{request.production.merge_branch}"
    try:
        commits = change_review.unit_commits(request.production.checkout, request.commit, previous,
                                             request.production.merge_branch)
    except change_review.ChangeReviewError as error:
        raise Stop("change review", "failed", str(error), "the showrunner will inspect the failed command") from error
    if refused := change_review.missing(commits):
        raise Stop("change review", "held", "; ".join(refused),
                   f"commit a checkpoint whose {change_review.TRAILER} lines describe the change and that names each "
                   + f"listed commit in a `{change_review.TRAILER}-Covers: <hash>` line, push it, and send its hash")
    report("change review", "ok", f"{len(commits)} unit commit(s) carry their {change_review.TRAILER} lines")
    return commits


def checkpoint_step_record(checkout: Path, merge_head: str, step: str) -> Path:
    """Completion belongs to this merge, not to a session's disposable scratch."""
    path = good(git(checkout, "rev-parse", "--git-path", f"checkpoint-completed/{merge_head}/{step}"), step)
    return checkout / path


def record_changes(request: MergeRequest, commits: list[change_review.Commit], merge_head: str) -> None:
    """Add one row per visible change to the production's list, then name any merge still missing rows."""
    doc = change_review.list_path(request.production.doc)
    if doc is None:
        report("change review", "ok", "the production doc has no Change review line; no rows written")
        return
    done = checkpoint_step_record(request.production.checkout, merge_head, "change-review")
    if done.exists():
        report("change review", "ok", "checkpoint changes already recorded")
        return
    merged = good(git(request.production.checkout, "rev-parse", f"--short={change_review.SHORT}", merge_head), "push")
    try:
        # A stop after the list write but before the receipt must not add it twice.
        changes = change_review.visible(commits)
        existing = change_review.pending(doc) if doc.exists() else []
        held = {row["Change"] for row in existing
                if any(sha.startswith(merged) or merged.startswith(sha)
                       for sha in cast(list[str], re.findall(r"\b[0-9a-f]{7,40}\b", row.get("Merged", ""))))}
        rows = [] if changes and all(item.change in held for item in changes) else change_review.append_rows(
            doc, changes, datetime.now(request.production.zone).date(), merged, request.production.doc,
            owners=change_review.visible_owners(commits, request.production.doc))
        done.parent.mkdir(parents=True, exist_ok=True)
        done.touch()
        drifted = change_review.drift(request.production.checkout, request.production.merge_branch,
                                      change_review.read(doc), change_review.LANDED,
                                      change_review.foreign_units(request.production.doc))
    except (change_review.ChangeReviewError, OSError) as error:
        report("change review", "failed", f"{doc} not written: {error}; add its rows by hand")
        return
    report("change review", "ok", f"{len(rows)} row(s) added to {doc}" + "".join(f"; {row}" for row in rows))
    for line in drifted:
        report("change review", "drift", line)


def merge_paths(checkout: Path, base: str, tip: str) -> tuple[str, ...]:
    """The paths a merge of `tip` into `base` changes. A three-dot diff picks one merge base, so after a
    criss-cross merge it also lists paths `base` already holds; the merged tree does not. A conflicted
    merge still writes a tree, with the conflicted paths in it, and the conflicts step holds that merge."""
    tree = git(checkout, "merge-tree", "--write-tree", base, tip)
    if tree.returncode not in (0, 1):
        _ = good(tree, "scope")
    output = good(git(checkout, "diff", "--name-only", base, tree.stdout.split("\n", 1)[0].strip()), "scope")
    return tuple(line for line in output.splitlines() if line)


def changed_paths(request: MergeRequest, base: str = "") -> tuple[str, ...]:
    """The paths the checkpoint changes from `base`, by default the merge branch's tip."""
    return merge_paths(request.production.checkout, base or request.production.merge_branch, request.commit)


def allowed(path: str, ownership: tuple[str, ...]) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") for item in ownership)


def _origin_merge_refs(request: MergeRequest) -> list[str]:
    return list(dict.fromkeys([
        f"origin/{request.production.merge_branch}",
        *change_review.reviewed_merge_refs(request.production.checkout, change_review.registered_productions),
    ]))


def _unit_paths(request: MergeRequest, paths: tuple[str, ...]) -> tuple[str, ...]:
    """Keep paths whose checkpoint content differs from every origin merge base."""
    if not paths:
        return ()
    own = set(paths)
    for ref in _origin_merge_refs(request):
        try:
            base = git(request.production.checkout, "merge-base", ref, request.commit)
            if base.returncode:
                continue
            changed = git(request.production.checkout, "diff", "--name-only", "--no-renames", "-z",
                          base.stdout.strip(), request.commit, "--", *paths)
            if changed.returncode:
                continue
        except OSError:
            continue
        own.intersection_update(changed.stdout.split("\0"))
    return tuple(path for path in paths if path in own)


def scope(request: MergeRequest, paths: tuple[str, ...]) -> None:
    staged = good(git(request.production.checkout, "diff", "--cached", "--name-only"), "scope")
    if staged:
        raise Stop("scope", "held", f"staged paths in merge checkout: {staged.replace(chr(10), ', ')}",
                   "the showrunner will clear unrelated staged paths, then rerun the checkpoint")
    own_paths = _unit_paths(request, paths)
    imported = len(paths) - len(own_paths)
    paths = own_paths
    if isinstance(request.kind, ShrinkCommit):
        plan = request.unit.plan.as_posix()
        next_items = str(Path(plan).with_name(Path(plan).stem + "-next.md"))
        unexpected = [path for path in paths if path not in (plan, next_items)]
        if unexpected or plan not in paths:
            raise Stop("scope", "held", f"shrink paths must be {plan} and optional {next_items}; got {', '.join(paths)}",
                       "commit only the plan doc and its next-items file, then resend the shrink")
    elif isinstance(request.kind, AsBuiltCommit):
        plan = request.unit.plan.as_posix()
        unexpected = [path for path in paths if not path.endswith(".md")]
        if unexpected or plan not in paths:
            raise Stop("scope", "held", f"an as-built commit carries only docs and must change or delete {plan}; "
                       + f"got {', '.join(paths)}",
                       "commit only the as-built docs and the plan's deletion, then resend the as-built")
    else:
        hubs = hub_paths(request.lines, request.unit.name)
        permitted = (*request.unit.owns, *request.also, *hubs)
        unexpected = [path for path in paths if not allowed(path, permitted)]
        if unexpected:
            raise Stop("scope", "held", f"outside Owns: {', '.join(unexpected)}",
                       f"explain why these paths changed: {', '.join(unexpected)}")
    report("scope", "ok", f"{len(paths)} changed paths are allowed"
           + (f"; {imported} came from origin merge branches" if imported else ""))


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


def into(path: PurePath, branch: str, why: str, unblocks: str) -> None:
    print(f"into: {path} ({branch}) — {why}; unblocks: {unblocks}", flush=True)


def merge_subject(request: MergeRequest) -> str:
    kind = (" shrink" if isinstance(request.kind, ShrinkCommit) else " as-built" if isinstance(request.kind, AsBuiltCommit)
            else "")
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
                                                  else "Closing as-built docs." if isinstance(request.kind, AsBuiltCommit)
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


def test_steps(production: Production, paths: tuple[str, ...],
               merge_tests: tuple[tuple[str, ...], ...]) -> list[tuple[str, list[str]]]:
    checkout = production.checkout
    packages: set[str] = set()
    examples: set[tuple[str, str]] = set()
    for raw in paths:
        path = checkout / raw
        for parent in (path.parent, *path.parents):
            if parent == checkout.parent:
                break
            manifest = parent / "Cargo.toml"
            if manifest.exists():
                # A workspace root's manifest names no package: a path under it, such as docs, has no tests.
                if "package" in tomllib.loads(manifest.read_text(encoding="utf-8")):
                    packages.add(parent.name)
                break
        match = re.search(r"(?:^|/)crates/([^/]+)/examples/([^/.]+)", raw)
        if match:
            examples.add((match.group(1), match.group(2)))
    verify = tool("verify.sh", Path.home() / ".claude/scripts/direct/verify.sh", "MERGE_CHECKPOINT_VERIFY")
    verify_command = [verify] if os.access(verify, os.X_OK) else ["bash", verify]
    steps = [(f"package {package}", [*verify_command, "test", package]) for package in sorted(packages)]
    steps.extend((f"example {package}/{name}", [*verify_command, "example", package, name])
                 for package, name in sorted(examples))
    steps.extend((f"merge test {index}", list(command)) for index, command in enumerate(merge_tests, 1))
    return steps


def run_test_command(command: list[str], checkout: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, cwd=checkout, capture_output=True, text=True, check=False)
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, "", f"{error}\n")


def undo_unpushed_merge(production: Production, merge_head: str) -> None:
    checkout = production.checkout
    head = good(git(checkout, "rev-parse", "HEAD"), "red")
    if head != merge_head:
        raise Stop("red", "failed", f"HEAD moved from unpushed merge {merge_head}",
                   "the showrunner will inspect the merge branch")
    if git(checkout, "merge-base", "--is-ancestor", head,
           f"origin/{production.merge_branch}").returncode == 0:
        raise Stop("red", "failed", "merge is already pushed; cannot reset it",
                   "the showrunner will inspect the merge branch")
    _ = good(git(checkout, "reset", "--keep", "HEAD~1"), "red")


def tests(request: MergeRequest, paths: tuple[str, ...], merge_head: str) -> str:
    if not isinstance(request.kind, CodeCheckpoint):
        report("test", "ok", f"{'shrink' if isinstance(request.kind, ShrinkCommit) else 'as-built'} has no package tests")
        return "no tests"
    return checkpoint_tests(request.production, paths, request.commit, request.merge_tests,
                            request.flakes, request.scratch, merge_head, request.flaky_tests)


# A nextest failure row: `FAIL [  12.2s] (1012/1067) <binary> <test>`.
NEXTEST_FAIL = re.compile(r"^\s+FAIL \[[^]]*\] \([^)]*\) \S+ (\S+)\s*$", re.MULTILINE)


def failed_tests(output: str) -> frozenset[str]:
    """The names of the tests nextest reports failed; each row is printed twice, so a set."""
    return frozenset(NEXTEST_FAIL.findall(output))


def only_tests(command: list[str], names: frozenset[str]) -> list[str]:
    """The command narrowed to the named tests: verify.sh --filter, else nextest -E test(=name)."""
    if any(Path(word).name == "verify.sh" for word in command):
        return [*command, *(word for name in sorted(names) for word in ("--filter", name))]
    return [*command, *(word for name in sorted(names) for word in ("-E", f"test(={name})"))]


def stop_cancelled_test(name: str, result: subprocess.CompletedProcess[str], log: Path) -> None:
    if result.returncode == TREE_CHANGED_STATUS:
        raise Stop("test", "cancelled", f"{name} cancelled because the tree changed; log {log}",
                   f"resubmit this checkpoint after your edits are done; test log: {log}")


def checkpoint_tests(production: Production, paths: tuple[str, ...], checkpoint: str,
                     merge_tests: tuple[tuple[str, ...], ...], flakes: frozenset[str], scratch: Path,
                     merge_head: str, flaky_tests: frozenset[str]) -> str:
    steps = test_steps(production, paths, merge_tests)
    scratch.mkdir(parents=True, exist_ok=True)
    log = scratch / f"merge_{checkpoint[:7]}_test.log"
    failed: list[tuple[str, list[str], frozenset[str]]] = []
    with log.open("w", encoding="utf-8") as output:
        for name, command in steps:
            result = run_test_command(command, production.checkout)
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{name.replace(' ', '_')}_EXIT={result.returncode}\n")
            output.flush()
            stop_cancelled_test(name, result, log)
            if not test_passed(command, result):
                failed.append((name, command, failed_tests(result.stdout + result.stderr)))
    unresolved: list[str] = []
    for name, command, tests in failed:
        # Only known flaky tests failed: rerun those alone, not the whole command.
        alone = bool(tests) and tests <= flaky_tests
        with log.open("a", encoding="utf-8") as output:
            result = run_test_command(only_tests(command, tests) if alone else command, production.checkout)
            _ = output.write(result.stdout + result.stderr)
            _ = output.write(f"{name.replace(' ', '_')}_RERUN_EXIT={result.returncode}\n")
        stop_cancelled_test(name, result, log)
        package = name.removeprefix("package ")
        if test_passed(command, result) and (alone or package in flakes):
            note = (f"known flaky tests {', '.join(sorted(tests))}: red in {name}, green alone" if alone
                    else f"known flake {package}: red in batch, green alone")
            production.log.parent.mkdir(parents=True, exist_ok=True)
            with production.log.open("a", encoding="utf-8") as ledger:
                _ = ledger.write(f"- {datetime.now(production.zone):%H:%M %Z}: {note}\n")
        else:
            unresolved.append(name)
    report("test", "ok", f"{len(steps)} commands ran; {len(failed)} red; log {log}")
    if unresolved:
        undo_unpushed_merge(production, merge_head)
        raise Stop("red", "failed", f"{', '.join(unresolved)} failed; log {log}",
                   f"repair {', '.join(unresolved)} and send a new checkpoint; test log: {log}")
    report("red", "ok", "all commands green or known flakes green alone")
    return ", ".join(name for name, _ in steps) if steps else "no tests"


def test_passed(command: list[str], result: subprocess.CompletedProcess[str]) -> bool:
    """Judge basedpyright as merge_gate.py does: status 0 or 3 and its clean summary line anywhere.

    It exits 3 and prints a `.venv` warning on stderr, after the summary, because pyrightconfig
    names a `.venv` no checkout has.
    """
    if Path(command[0]).name != "basedpyright":
        return result.returncode == 0
    output = f"{result.stdout}\n{result.stderr}"
    clean = re.search(r"^0 errors, 0 warnings(?:, 0 notes)?\s*$", output, re.MULTILINE)
    return result.returncode in (0, 3) and clean is not None


def undo_failed_push(production: Production, old_origin: str) -> None:
    checkout = production.checkout
    if git(checkout, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        _ = good(git(checkout, "merge", "--abort"), "push")
    remote = good(git(checkout, "ls-remote", "origin", f"refs/heads/{production.merge_branch}"), "push")
    if remote.split() and remote.split()[0] == old_origin:
        _ = good(git(checkout, "reset", "--keep", old_origin), "push")


def push_git(production: Production, may_undo: bool) -> str:
    checkout = production.checkout
    old_origin = good(git(checkout, "rev-parse", f"origin/{production.merge_branch}"), "push")
    try:
        _ = good(git(checkout, "fetch", "origin", "main"), "push")
        main = git(checkout, "rev-parse", "origin/main")
        if main.returncode == 0 and git(checkout, "merge-base", "--is-ancestor", "origin/main", "HEAD").returncode:
            _ = good(git(checkout, "merge", "--no-ff", "-m",
                         "Merge origin/main into " + production.merge_branch, "origin/main"), "push")
        tip = good(git(checkout, "rev-parse", "HEAD"), "push")
        _ = good(git(checkout, "push", "origin", production.merge_branch), "push")
    except Stop:
        if may_undo:
            undo_failed_push(production, old_origin)
        raise
    report("push", "ok", f"{production.merge_branch} pushed at {tip}")
    return tip


def pushed_state(production: Production, checkpoint: str) -> PushedTip | NotYetPushed:
    checkout = production.checkout
    branch = production.merge_branch
    _ = good(git(checkout, "fetch", "origin", branch), "push")
    remote = f"origin/{branch}"
    if git(checkout, "merge-base", "--is-ancestor", checkpoint, remote).returncode:
        return NotYetPushed()
    return PushedTip(good(git(checkout, "rev-parse", remote), "push"))


def push_validate(production: Production, fix_commit: str, may_undo: bool, cancel_prior: bool = False) -> str:
    script = tool("validate_and_push.sh", Path.home() / ".claude/scripts/validate_and_push/validate_and_push.sh",
                  "MERGE_CHECKPOINT_VALIDATE_AND_PUSH")
    command = [script, "--quick", "--to", production.merge_branch,
               "--fix-commit", fix_commit]
    if cancel_prior:
        command.append("--cancel-prior")
    old_origin = good(git(production.checkout, "rev-parse", f"origin/{production.merge_branch}"), "push")
    try:
        result = subprocess.run(command, cwd=production.checkout, capture_output=True, text=True, check=False)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise Stop("push", "failed", detail, "repair the push failure and send a new checkpoint")
    except (Stop, OSError):
        if may_undo:
            undo_failed_push(production, old_origin)
        raise
    tip = good(git(production.checkout, "rev-parse", "HEAD"), "push")
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


def remote_path(path: PurePosixPath) -> str:
    """Quote a Mac path for its shell, leaving a leading ~ for the Mac to expand to its own home."""
    text = str(path)
    if text == "~":
        return '"$HOME"'
    if text.startswith("~/"):
        return '"$HOME"/' + shlex.quote(text[2:])
    return shlex.quote(text)


def pull_mac_checkout(mac: PurePosixPath, source: Path) -> None:
    remote = (f"cd {remote_path(mac)} && git pull --ff-only "
              f"natedev:{shlex.quote(str(source))} main; printf 'rc=%s\\n' \"$?\"")
    result = subprocess.run(["ssh", "mac", remote], capture_output=True, text=True, check=False)
    match = re.search(r"(?:^|\n)rc=(\d+)", result.stdout)
    if result.returncode or match is None or int(match.group(1)):
        raise Stop("promote", "failed", f"mac {mac}: {result.stdout.strip()} {result.stderr.strip()}",
                   "the showrunner will repair the Mac pull and rerun this checkpoint")


def send_queued_zone() -> None:
    """Send a /zone change queued while the Mac was offline: the pull just done carried its commit."""
    done = subprocess.run([sys.executable, str(ZONE_SCRIPT), "drain", "--pulled"],
                          capture_output=True, text=True, check=False)
    for line in done.stdout.splitlines():
        report("promote", "ok", line)


def promote(request: MergeRequest | ChildHandoffRequest, tip: str) -> None:
    if isinstance(request.promotion, production_doc.NoPromotion):
        report("promote", "ok", "no promotion configured")
        return
    label = (f"{request.unit.name} phase {request.phase}" if isinstance(request, MergeRequest)
             else f"{request.child.slug} style hand-off")
    for checkout in request.promotion.checkouts:
        result = promote_local_checkout(checkout, request.production.merge_branch, tip)
        already_at = isinstance(result, (PromotionAlreadyAtTip, PromotionOriginPushed)) or (
            isinstance(result, PromotionNotPushed) and result.already_at_tip)
        detail = f"{checkout} {'already at' if already_at else 'main at'} {tip}"
        if isinstance(result, PromotionNotPushed):
            detail += "; main holds commits not on origin/" + request.production.merge_branch + " and was not pushed"
        report("promote", "ok", detail)
        into(checkout, "main", f"sessions on {os.uname().nodename} run the installed commands from it",
             f"the live gate of {label}")
    if isinstance(request.promotion.mac, production_doc.MacCheckout):
        mac = request.promotion.mac.path
        source = request.promotion.checkouts[0] if request.promotion.checkouts else request.production.checkout
        pull_mac_checkout(mac, source)
        report("promote", "ok", f"mac {mac} main pulled")
        send_queued_zone()
        into(mac, "main", "sessions on mac run the installed commands from it",
             f"the live gate of {label}")


def review_numbers(line: str) -> ReviewTrial:
    pattern = (r"review trial: ux (\d+) findings, code (\d+) findings, review-worker minutes ([\d.]+), "
               r"ux check minutes ([\d.]+), ux repair minutes ([\d.]+)")
    match = re.fullmatch(pattern, line.strip())
    if match is None:
        raise Refusal("--review-trial must read: review trial: ux <N> findings, code <N> findings, "
                      + "review-worker minutes <M>, ux check minutes <U>, ux repair minutes <R>")
    return ReviewTrial(*match.groups())


def check_parallel(line: str | None) -> None:
    """Refuse a code checkpoint whose notice lacks a usable `Parallel:` line."""
    if not line:
        raise Refusal("code checkpoint requires --parallel: the notice's line from phase_table.py parallel")
    text = line.strip()
    if text.startswith("Parallel: unknown"):
        raise Refusal("--parallel is unknown: the unit adds a **Depends on:** line under each waiting phase "
                      + "heading of its plan, reruns phase_table.py parallel and resends the notice")
    if re.fullmatch(r"Parallel: (Phase \S+(, Phase \S+)* could start|none — .+)", text) is None:
        raise Refusal("--parallel must read: Parallel: Phase <N>[, Phase <M>] could start, "
                      + "or Parallel: none — <what the next phase waits on>")


def record(request: MergeRequest, merge_head: str, tests_green: str) -> None:
    recorded = (f"{request.unit.name} phase {request.phase} ({request.commit}) merged as {merge_head}; ")
    if request.production.log.exists() and recorded in request.production.log.read_text(encoding="utf-8"):
        report("record", "ok", "checkpoint already recorded")
        return
    detail = ("review ledger and production log updated" if isinstance(request.kind, CodeCheckpoint)
              else f"{'shrink' if isinstance(request.kind, ShrinkCommit) else 'as-built'} logged without review-ledger row")
    if isinstance(request.kind, CodeCheckpoint):
        numbers = request.review_trial
        if isinstance(numbers, NoReviewTrial):
            raise Stop("record", "failed", "review trial absent", "send the notice's full review trial line")
        merged = good(git(request.production.checkout, "show", "-s", "--format=%cI", merge_head), "record")
        script = tool("review_regime.py", Path.home() / ".claude/scripts/production/review_regime.py",
                      "MERGE_CHECKPOINT_REVIEW_REGIME")
        review_command = [script] if os.access(script, os.X_OK) else [sys.executable, script]
        command = [*review_command, "add", "--unit", request.unit.name, "--plan", request.unit.plan.as_posix(),
                   "--phase", request.phase, "--regime", request.regime, "--started", request.started, "--merged", merged,
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
        if result.stdout.startswith("kept "):
            detail = result.stdout.strip() + "; " + detail
    request.production.log.parent.mkdir(parents=True, exist_ok=True)
    with request.production.log.open("a", encoding="utf-8") as log:
        suffix = "; promoted" if isinstance(request.promotion, production_doc.PromoteTo) else ""
        _ = log.write(f"- {datetime.now(request.production.zone):%H:%M %Z}: {request.unit.name} phase {request.phase} "
                      + f"({request.commit}) merged as {merge_head}; {tests_green} green; pushed{suffix}\n")
    report("record", "ok", detail)


def end_phase_scratch(request: MergeRequest) -> None:
    """Remove the merged phase's scratch folders; a failure warns and never fails the merge."""
    if not isinstance(request.kind, CodeCheckpoint):
        return
    task = f"phase:{request.unit.name}/{request.phase}"
    try:
        removed = scratch.end(task)
    except (scratch.ScratchError, OSError) as error:
        report("scratch", "warning", f"{task} not removed: {error}")
        return
    if removed:
        report("scratch", "ok", f"removed {len(removed)} folder(s) of {task}: " + ", ".join(map(str, removed)))


def notify_style(checkout: Path, branch: str, tip: str) -> None:
    try:
        script = tool("style_sync.py", Path(__file__).resolve().parents[1] / "style/style_sync.py",
                      "MERGE_CHECKPOINT_STYLE_SYNC")
        python = shutil.which("python3") or sys.executable
        _ = sys.stdout.flush()
        result = subprocess.run(
            [python, script, "notify", "--repo-dir", str(checkout), "--branch", branch, "--tip", tip],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode or result.stdout.strip() or result.stderr.strip():
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            print("style sync: " + detail.replace("\n", "; "), flush=True)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("style sync: " + str(error).replace("\n", "; "), flush=True)


@contextmanager
def _external_merge_wait(arguments: list[str]) -> Generator[None, None, None]:
    if os.environ.get(PROBE_ENV) == "1":
        yield
        return
    recorder = [sys.executable, str(Path(progress_history.__file__))]
    wait_id = ""
    try:
        started = subprocess.run([*recorder, *arguments], capture_output=True, text=True, check=False)
        wait_id = started.stdout.strip() if started.returncode == 0 else ""
        if not wait_id:
            report("wait", "NotRecorded", started.stderr.strip() or "recorder returned no wait id")
    except OSError as error:
        report("wait", "NotRecorded", str(error))
    try:
        yield
    finally:
        if wait_id:
            try:
                finished = subprocess.run([*recorder, "finish-external-wait", "--wait-id", wait_id],
                                          capture_output=True, text=True, check=False)
                if finished.returncode:
                    report("wait", "NotRecorded", finished.stderr.strip() or "cannot close merge gate wait")
            except OSError as error:
                report("wait", "NotRecorded", str(error))


@contextmanager
def merge_gate_wait(worktree: Path, phase_id: str, unit: str, checkpoint: str) -> Generator[None, None, None]:
    """Resolve the full checkpoint in durable history, including finished runs."""
    with _external_merge_wait(["start-checkpoint-wait", "--commit", checkpoint,
        "--working-dir", str(worktree), "--phase-id", phase_id, "--unit", unit,
        "--reason", "checkpoint merge tests"]):
        yield


@contextmanager
def child_handoff_wait(production: str, checkpoint: str) -> Generator[None, None, None]:
    """A child branch checkpoint belongs to its production, with no phase."""
    owner = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID", "")
    with _external_merge_wait(["start-handoff-wait", "--production", production,
        "--commit", checkpoint, "--owner-session", owner, "--reason", "child production merge tests"]):
        yield


def record_pushed_merges(production: Production, merge_head: str, source_checkpoint: str,
                         tip: str, *, phase_id: str, issue: str = "") -> None:
    """Receipts survive promotion failures and include origin/main integration merges."""
    repository = production_field(production.doc.read_text(encoding="utf-8").splitlines(), "Repository")
    project = Path(repository).expanduser().name
    merges = good(git(production.checkout, "rev-list", "--first-parent", "--merges", "--reverse",
                      f"{merge_head}^1..{tip}"), "receipt").splitlines()
    for commit in merges:
        if commit != merge_head and good(git(production.checkout, "show", "-s", "--format=%s", commit), "receipt") != (
            "Merge origin/main into " + production.merge_branch
        ):
            break  # A retry can see later units' merges; their own call records them.
        source = source_checkpoint if commit == merge_head else good(
            git(production.checkout, "rev-parse", f"{commit}^2"), "receipt")
        stamp = float(good(git(production.checkout, "show", "-s", "--format=%ct", commit), "receipt"))
        _ = progress_history.write_merge_receipt(
            project=project, phase_id=phase_id, issue=issue, merge_commit=commit,
            source_checkpoint=source, target_branch=production.merge_branch, merged_at=stamp,
        )


def run(request: MergeRequest) -> int:
    try:
        checkpoint = git(request.unit.worktree, "rev-parse", "--verify", f"{request.commit}^{{commit}}")
        if checkpoint.returncode:
            raise Stop("ancestry", "failed", f"commit {request.commit} is absent",
                       "push the checkpoint and send its hash again")
        request = request._replace(commit=checkpoint.stdout.strip())
        already = ancestry(request)
        tests_green = "merge tests" if isinstance(request.kind, CodeCheckpoint) else "no tests"
        pushed = pushed_state(request.production, request.commit) if already else NotYetPushed()
        commits = [] if already else change_review_commits(request)
        if not already:
            paths = changed_paths(request)
            scope(request, paths)
            conflicts(request)
            other_units(request, paths)
            merge_head = merge(request)
        else:
            merge_head = merged_checkpoint(request)
            # Origin and last-for-unit already contain this checkpoint. Its merge
            # parents retain the original incoming commits even after later merges.
            commits = change_review.merge_commits(request.production.checkout, merge_head)
            # A run killed between its merge and its push left a merge nothing has tested.
            steps = ("scope", "conflicts", "other units", "merge")
            if isinstance(pushed, PushedTip):
                steps += ("test", "red")
            for step in steps:
                report(step, "ok", "checkpoint already merged; resuming promotion")
            paths = changed_paths(request, f"{merge_head}^1")
        if isinstance(pushed, NotYetPushed):
            try:
                if isinstance(request.kind, CodeCheckpoint):
                    with merge_gate_wait(request.unit.worktree, request.phase, request.unit.name, request.commit):
                        tests_green = tests(request, paths, merge_head)
                else:
                    tests_green = tests(request, paths, merge_head)
            except (OSError, ValueError) as error:
                undo_unpushed_merge(request.production, merge_head)
                raise Stop("red", "failed", str(error),
                           "repair the test logging failure and send a new checkpoint") from error
        first_code_merge = isinstance(pushed, NotYetPushed) and isinstance(request.kind, CodeCheckpoint)
        if isinstance(pushed, PushedTip):
            tip = pushed.tip
            report("push", "ok", f"{request.production.merge_branch} already pushed at {tip}")
        elif isinstance(request.push, GitPush):
            tip = push_git(request.production, not already)
        else:
            tip = push_validate(request.production,
                                f"ci({request.production.slug}): format fixes after {request.unit.name} phase {request.phase}",
                                not already, request.cancel_prior)
        record_pushed_merges(request.production, merge_head, request.commit, tip, phase_id=request.phase,
                             issue=str(request.unit.plan))
        notify_style(request.production.checkout, request.production.merge_branch, tip)
        record_changes(request, commits, merge_head)
        into(request.production.checkout, request.production.merge_branch,
             "the merge branch collects every unit's checkpoints", gate_names(request, first_code_merge))
        promote(request, tip)
        record(request, merge_head, tests_green)
        end_phase_scratch(request)
        print(f"send {request.unit.name}: phase {request.phase} merged and pushed as {merge_head}; continue with the next step")
        return 0
    except Stop as error:
        report(error.step, error.state, error.detail.replace("\n", "; "))
        print(f"send {request.unit.name}: {error.message}")
        return TREE_CHANGED_STATUS if error.state == "cancelled" else 1
    except (OSError, ValueError) as error:
        report("run", "failed", str(error).replace("\n", "; "))
        print(f"send {request.unit.name}: the showrunner will inspect the failed command")
        return 1


class NotMerged(NamedTuple):
    pass


class MergedNotPushed(NamedTuple):
    merge_head: str


class MergedAndPushed(NamedTuple):
    merge_head: str


class ChildHandoffRequest(NamedTuple):
    production: Production
    child: Production
    checkpoint: str
    push: ValidateAndPush | GitPush
    merge_tests: tuple[tuple[str, ...], ...]
    started: str
    delivery: NotMerged | MergedNotPushed | MergedAndPushed
    promotion: production_doc.NoPromotion | production_doc.PromoteTo
    root_self: bool
    flakes: frozenset[str]
    flaky_tests: frozenset[str]
    scratch: Path


def child_runner(action: str, *arguments: str) -> str:
    script = tool("showrunners.py", Path(__file__).with_name("showrunners.py"), "MERGE_CHECKPOINT_SHOWRUNNERS")
    result = subprocess.run(
        [sys.executable, script, action, *arguments],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"showrunners.py {action} exited {result.returncode}")
    return result.stdout.strip()


def child_request(args: argparse.Namespace) -> ChildHandoffRequest:
    production = read_production(Path(cast(str, args.production)))
    slug = canonical_slug(cast(str, args.from_child))
    try:
        parent = child_runner("parent", slug)
    except Refusal as error:
        raise Refusal(f"{slug} is not attached under {production.slug}") from error
    root_self = cast(bool, args.root_self)
    if root_self and parent:
        raise Refusal(f"{slug} reports to {parent}; --root-self needs a root")
    if not root_self and parent != production.slug:
        raise Refusal(f"{slug} is not attached under {production.slug}")
    doc = next((row.split("\t")[-1] for row in child_runner("list").splitlines()
                if row.split("\t")[0] == slug), "")
    if not doc:
        raise Refusal(f"no production doc for child {slug}")
    child = read_production(Path(doc).expanduser())
    lines = child.doc.read_text().splitlines()
    if production_field(lines, "Kind") != "style":
        raise Refusal(f"{slug} is not a style production")
    branch_point = production_field(lines, "Branch point").removeprefix("refs/heads/")
    if branch_point != production.merge_branch.removeprefix("refs/heads/"):
        raise Refusal(f"{slug} follows branch point {branch_point}, not {production.merge_branch}")
    checkout = production.checkout
    common = good(git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"), "input")
    if common != good(git(child.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"), "input"):
        raise Refusal(f"{slug} is not in the parent's repository")
    checkpoint = good(git(checkout, "rev-parse", "--verify", f"{cast(str, args.checkpoint)}^{{commit}}"), "ancestry")
    if git(child.checkout, "merge-base", "--is-ancestor", checkpoint, child.merge_branch).returncode:
        raise Refusal(f"checkpoint {checkpoint[:7]} is not on {slug}'s merge branch {child.merge_branch}")
    history = merge_branch_history(checkout, production.merge_branch)
    last = history.last_handoff_for_child(slug)
    already = git(checkout, "merge-base", "--is-ancestor", checkpoint, production.merge_branch).returncode == 0
    if isinstance(last, ChildHandoffEntry) and not already:
        previous = good(git(checkout, "rev-parse", "--verify", last.merge_hash + "^2"), "ancestry")
        if git(checkout, "merge-base", "--is-ancestor", previous, checkpoint).returncode:
            raise Refusal(f"checkpoint {checkpoint[:7]} does not descend from {slug}'s last merged hand-off {last.short}")
    parent_lines = production.doc.read_text().splitlines()
    mode = production_doc.optional_field(parent_lines, "Push")
    raw = cell_value(mode.text) if isinstance(mode, production_doc.FieldPresent) else "validate_and_push"
    if raw not in ("git", "validate_and_push"):
        raise Refusal(f"unknown Push mode: {raw}")
    delivery: NotMerged | MergedNotPushed | MergedAndPushed = NotMerged()
    if already:
        merge_head = handoff_merge_evidence(production, slug, checkpoint)
        pushed = pushed_state(production, checkpoint)
        delivery = MergedAndPushed(merge_head) if isinstance(pushed, PushedTip) else MergedNotPushed(merge_head)
    promotion = production_doc.promotion_from_doc(parent_lines)
    if isinstance(promotion, production_doc.PromoteTo):
        for destination in promotion.checkouts:
            if not destination.is_dir():
                raise Refusal(f"Promote checkout {destination} does not exist; name an existing checkout, or none")
    flakes = listed_names(parent_lines, "Known flakes")
    flaky_tests = listed_names(parent_lines, "Known flaky tests")
    scratch = Path(os.environ.get("MERGE_CHECKPOINT_SCRATCH") or tempfile.gettempdir()).expanduser()
    return ChildHandoffRequest(production, child, checkpoint, GitPush() if raw == "git" else ValidateAndPush(),
                               parse_merge_tests(parent_lines, production.checkout), cast(str, args.started), delivery, promotion,
                               root_self, flakes, flaky_tests, scratch)


def handoff_merge_evidence(production: Production, child: str, checkpoint: str) -> str:
    checkout, branch = production.checkout, production.merge_branch
    history = merge_branch_history(checkout, branch)
    matching = next((entry for entry in history.entries if isinstance(entry, ChildHandoffEntry)
                     and entry.child == child and checkpoint.startswith(entry.short)), None)
    if matching is not None:
        return matching.merge_hash
    for commit in good(git(checkout, "rev-list", "--first-parent", "--reverse", "--merges", branch), "merge").splitlines():
        if git(checkout, "merge-base", "--is-ancestor", checkpoint, commit).returncode == 0:
            return commit
    return good(git(checkout, "rev-parse", branch), "merge")


def record_handoff_merge(request: ChildHandoffRequest, merge_head: str) -> None:
    try:
        script = tool("style_ledger.py", Path(__file__).resolve().parents[1] / "style/style_ledger.py",
                      "MERGE_CHECKPOINT_STYLE_LEDGER")
        python = shutil.which("python3") or sys.executable
        _ = sys.stdout.flush()
        result = subprocess.run(
            [python, script, "handoff", "merged", "--repo", str(request.child.checkout),
             "--checkpoint", request.checkpoint, "--merge-commit", merge_head],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            print("hand-off ledger: " + detail.replace("\n", "; "), flush=True)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("hand-off ledger: " + str(error).replace("\n", "; "), flush=True)


def acknowledge_handoff(request: ChildHandoffRequest, merge_head: str) -> None:
    script = tool("send.py", Path(__file__).resolve().parents[1] / "message/send.py", "MERGE_CHECKPOINT_SEND")
    try:
        address = child_runner("address", request.child.slug)
        if not address:
            raise Refusal(f"no address for {request.child.slug}")
        result = subprocess.run([
            sys.executable, script, "--to", address, "--from", request.production.slug,
            "--text", f"From the showrunner of {request.production.slug}: hand-off {request.checkpoint[:7]} merged as {merge_head[:7]}.",
            "--timeout", "10",
        ], capture_output=True, text=True, check=False, timeout=15)
        if result.returncode:
            raise Refusal(result.stderr.strip() or result.stdout.strip() or f"send.py exited {result.returncode}")
    except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("hand-off acknowledgment: " + " ".join(str(error).splitlines()), flush=True)


def finish_handoff(request: ChildHandoffRequest, merge_head: str) -> None:
    if request.root_self:
        print(f"root: run style_ledger.py reconcile to mark {request.checkpoint[:7]} merged", flush=True)
    else:
        record_handoff_merge(request, merge_head)
        acknowledge_handoff(request, merge_head)


def handoff_tests_pending_path(checkout: Path, checkpoint: str) -> Path:
    """Keep untested handoffs across cancelled runs, scratch cleanup, and new sessions."""
    path = good(git(checkout, "rev-parse", "--git-path", f"checkpoint-tests-pending/{checkpoint}"), "test")
    return checkout / path


def promote_handoff(request: ChildHandoffRequest, merge_head: str, tip: str) -> None:
    """Retry interrupted promotion; a push receipt is not a promotion receipt."""
    if isinstance(request.promotion, production_doc.NoPromotion):
        return
    done = checkpoint_step_record(request.production.checkout, merge_head, "handoff-promotion")
    if not done.exists():
        promote(request, tip)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.touch()


def record_handoff_completion(request: ChildHandoffRequest, merge_head: str, tip: str) -> None:
    entry = f"{request.child.slug} style hand-off ({request.checkpoint}) merged as {merge_head}; pushed at {tip}"
    log = request.production.log
    if log.exists() and entry in log.read_text(encoding="utf-8"):
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as output:
        _ = output.write(f"- {datetime.now(request.production.zone):%H:%M %Z}: {entry}\n")


def run_handoff(request: ChildHandoffRequest) -> int:
    checkout, branch, checkpoint = request.production.checkout, request.production.merge_branch, request.checkpoint
    delivery = request.delivery
    if isinstance(delivery, MergedAndPushed):
        print(f"hand-off {checkpoint[:7]} already merged as {delivery.merge_head[:7]}", flush=True)
        pushed = pushed_state(request.production, checkpoint)
        tip = pushed.tip if isinstance(pushed, PushedTip) else delivery.merge_head
        record_pushed_merges(request.production, delivery.merge_head, checkpoint, tip,
                             phase_id="", issue=f"{request.child.slug} hand-off")
        promote_handoff(request, delivery.merge_head, tip)
        record_handoff_completion(request, delivery.merge_head, tip)
        finish_handoff(request, delivery.merge_head)
        return 0
    current = good(git(checkout, "branch", "--show-current"), "merge")
    if current != branch:
        raise Refusal(f"merge checkout is on {current}, expected {branch}")
    staged = good(git(checkout, "diff", "--cached", "--name-only"), "merge")
    if staged:
        raise Refusal(f"staged paths in merge checkout: {staged.replace(chr(10), ', ')}")
    pending_tests = handoff_tests_pending_path(checkout, checkpoint)
    paths: tuple[str, ...] = ()
    if isinstance(delivery, NotMerged):
        paths = merge_paths(checkout, branch, checkpoint)
        _ = good(git(checkout, "merge-tree", "--write-tree", branch, checkpoint), "conflicts")
        subject = f"Merge {request.child.slug} hand-off ({checkpoint[:7]}) into {branch}"
        pending_tests.parent.mkdir(parents=True, exist_ok=True)
        pending_tests.touch()
        _ = good(git(checkout, "merge", "--no-ff", "-m", subject, checkpoint), "merge")
        merge_head = good(git(checkout, "rev-parse", "HEAD"), "merge")
    else:
        merge_head = delivery.merge_head
        if pending_tests.exists():
            paths = merge_paths(checkout, f"{merge_head}^1", checkpoint)
    if isinstance(delivery, NotMerged) or pending_tests.exists():
        try:
            with child_handoff_wait(request.child.slug, checkpoint):
                _ = checkpoint_tests(request.production, paths, checkpoint, request.merge_tests,
                                     request.flakes, request.scratch, merge_head, request.flaky_tests)
        except (OSError, ValueError) as error:
            undo_unpushed_merge(request.production, merge_head)
            raise Stop("red", "failed", str(error), "repair the test logging failure and send a new checkpoint") from error
        pending_tests.unlink()
    if isinstance(request.push, GitPush):
        tip = push_git(request.production, isinstance(delivery, NotMerged))
    else:
        tip = push_validate(request.production,
                            f"ci({request.production.slug}): format fixes after {request.child.slug} style hand-off",
                            isinstance(delivery, NotMerged))
    record_pushed_merges(request.production, merge_head, checkpoint, tip,
                         phase_id="", issue=f"{request.child.slug} hand-off")
    notify_style(checkout, branch, tip)
    if isinstance(request.promotion, production_doc.NoPromotion):
        promote(request, tip)
    promote_handoff(request, merge_head, tip)
    record_handoff_completion(request, merge_head, tip)
    finish_handoff(request, merge_head)
    print(f"hand-off {checkpoint[:7]} merged as {merge_head[:7]}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("unit", nargs="?")
    _ = parser.add_argument("phase", nargs="?")
    _ = parser.add_argument("hash", nargs="?")
    _ = parser.add_argument("--from-child")
    _ = parser.add_argument("--checkpoint")
    _ = parser.add_argument("--root-self", action="store_true")
    _ = parser.add_argument("--also", action="append", default=[])
    _ = parser.add_argument("--shrink", action="store_true")
    _ = parser.add_argument("--as-built", action="store_true")
    _ = parser.add_argument("--trailer", action="append", default=[])
    _ = parser.add_argument("--review-trial")
    _ = parser.add_argument("--parallel")
    _ = parser.add_argument("--delivers")
    _ = parser.add_argument("--cancel-prior", action="store_true")
    _ = parser.add_argument("--started", required=True)
    _ = parser.add_argument("--regime", choices=("after", "trial"), default="after")
    _ = parser.add_argument("--holds", type=int, default=0)
    _ = parser.add_argument("--merge-defects", type=int, default=0)
    _ = parser.add_argument("--excluded")
    _ = parser.add_argument("--scratch")
    args = parser.parse_args()
    if cast(str | None, args.from_child):
        try:
            if not cast(str | None, args.checkpoint):
                raise Refusal("--from-child requires --checkpoint")
            if any((cast(str | None, args.unit), cast(str | None, args.phase), cast(str | None, args.hash),
                    cast(bool, args.shrink), cast(bool, args.as_built), cast(str | None, args.review_trial),
                    cast(str | None, args.parallel), cast(str | None, args.delivers), cast(bool, args.cancel_prior),
                    cast(list[str], args.also), cast(list[str], args.trailer),
                    cast(str, args.regime) != "after", cast(int, args.holds), cast(int, args.merge_defects),
                    cast(str | None, args.excluded), cast(str | None, args.scratch))):
                raise Refusal("--from-child is separate from unit checkpoint arguments")
            handoff = child_request(args)
            with merge_lock(handoff.production.checkout, handoff.production.slug):
                return run_handoff(handoff)
        except Stop as error:
            if error.state == "cancelled":
                report(error.step, error.state, error.detail.replace("\n", "; "))
                print(error.message, flush=True)
                return TREE_CHANGED_STATUS
            print("merge_checkpoint: " + " ".join(str(error).splitlines()), flush=True)
            return 1
        except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print("merge_checkpoint: " + " ".join(str(error).splitlines()), flush=True)
            return 1
    if cast(bool, args.root_self) or cast(str | None, args.checkpoint):
        parser.error("--root-self and --checkpoint require --from-child")
    if not all((cast(str | None, args.unit), cast(str | None, args.phase), cast(str | None, args.hash))):
        parser.error("unit, phase and hash are required for a unit checkpoint")
    try:
        request = request_from(args)
        with merge_lock(request.production.checkout, request.production.slug):
            return run(request)
    except (Refusal, OSError, ValueError) as error:
        report("input", "failed", str(error))
        print(f"send {cast(str, args.unit)}: correct the checkpoint input and resend it")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
