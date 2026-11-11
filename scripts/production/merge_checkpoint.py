#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Merge, test, push, promote, and record one production checkpoint.

A unit checkpoint commit without its Change-Review line is held before the merge. After the push,
each visible change becomes a row in the change-review list of the production's top-level showrunner
(`change_review.py`), with the doc stamped into its frontmatter; the first row creates a missing list, and the list commits itself when it sits in a git
work tree. A refused commit warns and never fails the merge. A recorded code checkpoint ends its
phase: the scratch folders of task `phase:<unit>/<phase>` (scripts/lib/scratch.py) are removed.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import bisect
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import datetime
from pathlib import Path, PurePath, PurePosixPath
from typing import NamedTuple, cast

from scripts.production import change_review, checkpoint_gate
from scripts.worktree import worktrees
from scripts.lib import main_publish, scratch
from scripts.production.showrunners import owner_for
from scripts.production.production_doc import Production, ProductionDocument, Refusal, cell_value, production_field, read_production

from scripts.direct import progress_history  # noqa: E402
from scripts.production import merge_gate, production_doc, session_staff, staff, unit_config, unit_lookup, unit_names  # noqa: E402
from scripts.agents import remote  # noqa: E402
from scripts.lib.probe_session import PROBE_ENV  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.lib.process_failure import failure_detail  # noqa: E402
from scripts.lib import conf  # noqa: E402
from scripts.message import session_send  # noqa: E402

# /zone queues a change for the Mac while it is offline; a promotion's Mac pull sends it.
ZONE_SCRIPT = Path(__file__).resolve().parents[1] / "lib/zone.py"
PYTHON = str(Path(__file__).resolve().parents[1] / "lib/py")


class ValidateAndPush(NamedTuple):
    pass


class GitPush(NamedTuple):
    pass


class PushedTip(NamedTuple):
    tip: str


class NotYetPushed(NamedTuple):
    pass


class CheckpointOrigin(NamedTuple):
    branch: str
    already_merged: bool


class PromotionAlreadyAtTip(NamedTuple):
    tip: str


class PromotionAdvanced(NamedTuple):
    tip: str


class PromotionOriginPushed(NamedTuple):
    tip: str


class PromotionNotPushed(NamedTuple):
    tip: str
    already_at_tip: bool


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
    staff_id: staff.StaffId


class UnitUnavailable(NamedTuple):
    name: str
    reason: str


class MergeRequest(NamedTuple):
    production: Production
    unit: Unit
    phase: str
    commit: str
    kind: CodeCheckpoint | ShrinkCommit | AsBuiltCommit
    push: ValidateAndPush | GitPush
    promotion: production_doc.NoPromotion | production_doc.PromoteTo
    merge_tests: tuple[tuple[str, ...], ...]
    flakes: frozenset[str]
    flaky_tests: frozenset[str]
    units: tuple[Unit | UnitUnavailable, ...]
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
    bump: bool = False
    staff_record: staff.StaffSnapshot | None = None


def end_pushed_worktrees(production: ProductionDocument) -> None:
    for result in worktrees.end_landed(production.checkout, production.merge_branch):
        if isinstance(result, worktrees.Ended):
            checkpoint_gate.report("worktrees", "ended", f"{result.path}; branch {result.branch}")
        else:
            checkpoint_gate.report("worktrees", "kept", f"{result.path}: {result.reason}".replace("\n", "; "))


@contextmanager
def merge_lock(checkout: Path, name: str) -> Generator[None]:
    """Held for a whole merge gate. A unit rename takes it before it writes the merge branch, so
    neither lands a commit inside the other's run."""
    common = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        raise OSError(f"{checkout}: no git directory for the merge lock: {common.stderr.strip()}")
    with (Path(common.stdout.strip()) / f"showrunner-merge-{name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"merge lock: waiting — a merge gate or a unit rename is writing {name}'s merge branch", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, check=False)


@contextmanager
def main_push_lock(checkout: Path) -> Generator[None]:
    """Serialize the final main fetch, merge-branch push and main promotion."""
    common = good(git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"), "push")
    with (Path(common) / "main-push.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.monotonic()
        try:
            yield
        finally:
            seconds = time.monotonic() - started
            try:
                _ = lock.seek(0)
                _ = lock.truncate()
                _ = lock.write(json.dumps({"held_seconds": seconds}) + "\n")
                lock.flush()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def good(result: subprocess.CompletedProcess[str], step: str) -> str:
    if result.returncode:
        raise checkpoint_gate.Stop(step, "failed", failure_detail(result),
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


def unit_from_row(row: staff.StaffRow, resolver: session_staff.RowResolver) -> Unit | UnitUnavailable:
    """A local unit from its staff row, its branch and its `.unit.toml`."""
    name = row.name
    location = row.worktree
    if not isinstance(location, staff.PresentWorktree) or location.machine != remote.local_machine():
        return UnitUnavailable(name, "the worktree is not on this machine")
    branch = resolver.unit_branch(row)
    if not isinstance(branch, session_staff.Branch):
        return UnitUnavailable(name, branch.reason)
    try:
        config = resolver.unit_config(row)
    except unit_config.ConfigRefused as error:
        return UnitUnavailable(name, str(error))
    if isinstance(config, session_staff.NoUnitConfig):
        return UnitUnavailable(name, "no unit configuration")
    if not isinstance(config, unit_config.UnitConfig):
        return UnitUnavailable(name, config.reason)
    plan = (config.plan.path if isinstance(config.plan, unit_config.PlanFile)
            else Path("standby" if isinstance(config.plan, unit_config.StandbyPlan) else "permanent"))
    return Unit(name, plan, location.path, branch.name, row.id)


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


def user_bump(args: argparse.Namespace) -> bool:
    bump = cast(bool, getattr(args, "bump", False))
    if bump and any((cast(bool, args.shrink), cast(bool, args.as_built),
                     cast(str | None, getattr(args, "from_child", None)))):
        raise Refusal("--bump applies only to a code checkpoint requested by the user")
    return bump


def request_from(args: argparse.Namespace) -> MergeRequest:
    bump = user_bump(args)
    record = staff.snapshot()
    production = read_production(cast(str, args.production), record)
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    name = cast(str, args.unit)
    resolver = session_staff.RowResolver(record)
    rows = tuple(record.find(kind="unit", parent=production.owner.id))
    selected = next((row for row in rows if row.name == unit_names.name(name)), None)
    selected = selected if selected is not None else unit_lookup.unit_row(name, record)
    if selected.parent != staff.ReportsTo(production.owner.id):
        parent = record.parent_of(selected)
        raise Refusal(f"unit {name}: staff parent is {parent.name if parent else 'unavailable'}; use that production")
    units = tuple(unit_from_row(row, resolver) for row in rows
                  if not isinstance(row.worktree, staff.RemovedWorktree))
    unit = next((candidate for candidate in units if candidate.name == name), None)
    if unit is None:
        raise Refusal(f"unit {name} is not a staff unit of this production")
    if isinstance(unit, UnitUnavailable):
        raise Refusal(f"unit {name}: {unit.reason}")
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
                        kind, push, promotion,
                        parse_merge_tests(lines, production.checkout), flakes, flaky_tests, units, scratch,
                        tuple(cast(list[str], args.trailer)), review_trial, delivers or "",
                        cast(bool, args.cancel_prior), cast(str, args.started),
                        cast(str, args.regime), cast(int, args.holds), cast(int, args.merge_defects),
                        cast(str | None, args.excluded) or "", bump, record)


class MergedCheckpoint(NamedTuple):
    unit: str
    phase: str
    commit: str


def merged_checkpoints(production: Production, *, record: staff.StaffSnapshot | None = None) -> tuple[MergedCheckpoint, ...]:
    """Git proves which recorded unit checkpoints reached the merge branch."""
    record = record if record is not None else staff.snapshot()
    units = {row.worktree.path.resolve(): row.name for row in record.find(kind="unit", parent=production.owner.id)
             if not isinstance(row.worktree, staff.WorktreeUnfilled)}
    commits = good(git(production.checkout, "rev-list", "--reverse", "--topo-order",
                       production.merge_branch), "ancestry").splitlines()
    ancestors = set(commits)
    found: dict[str, MergedCheckpoint] = {}
    for event in progress_history.events_for_worktrees(tuple(units), frozenset({"checkpoint"})):
        commit, phase = event.get("checkpoint_commit"), event.get("phase_id")
        if isinstance(commit, str) and isinstance(phase, str) and commit in ancestors:
            found[commit] = MergedCheckpoint(units[Path(str(event["working_dir"]))], phase, commit)
    return tuple(found[commit] for commit in commits if commit in found)


def on_origin(request: MergeRequest) -> str | None:
    """Find an origin branch holding the checkpoint, trying the unit branch first."""
    checkout = request.production.checkout
    fetched = git(checkout, "fetch", "origin", request.unit.branch)
    # A missing unit branch is not a failure: another head may hold this phase.
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


def ancestry(request: MergeRequest) -> CheckpointOrigin:
    checkout = request.production.checkout
    origin_branch = on_origin(request)
    if origin_branch is None:
        raise checkpoint_gate.Stop("ancestry", "held", "commit is not on origin",
                   "push your branch to origin and resend the checkpoint")
    already = git(checkout, "merge-base", "--is-ancestor", request.commit,
                  request.production.merge_branch).returncode == 0
    if not isinstance(request.kind, CodeCheckpoint):
        _ = good(git(checkout, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"), "ancestry")
        closing = good(git(checkout, "rev-list", "--first-parent", "--no-merges", "-n", "1",
                           request.commit), "ancestry")
        unmerged = good(git(checkout, "rev-list", "--no-merges",
                            f"{request.production.merge_branch}..{request.commit}", "^origin/main"), "ancestry").splitlines()
        if any(commit != closing for commit in unmerged):
            raise checkpoint_gate.Stop("ancestry", "held", f"phase {request.phase} code checkpoint has not merged",
                       "wait for the phase code checkpoint to merge, then resend the shrink")
    checkpoint_gate.report("ancestry", "ok", f"checkpoint exists and is on {origin_branch}" +
           ("; already merged" if already else ""))
    return CheckpointOrigin(origin_branch.removeprefix("origin/"), already)


def branch_is_landed(checkout: Path, tip: str, target: str) -> bool:
    """Accept an ancestor or commits with equal patches on the target."""
    ancestor = git(checkout, "merge-base", "--is-ancestor", tip, target)
    if ancestor.returncode == 0:
        return True
    if ancestor.returncode != 1:
        _ = good(ancestor, "branch")
    patches = good(git(checkout, "cherry", target, tip), "branch")
    return all(line.startswith("- ") for line in patches.splitlines())


def branch_keep_reason(request: MergeRequest, branch: str) -> str:
    """Keep main, production merge branches, and branches with a present owner."""
    if branch == "main":
        return "main is long-lived"
    if branch == request.production.merge_branch:
        return "the production merge branch is long-lived"
    record = request.staff_record if request.staff_record is not None else staff.snapshot()
    resolver = session_staff.RowResolver(record)
    for row in record.rows:
        if isinstance(row.worktree, staff.PresentWorktree):
            owner_branch = resolver.unit_branch(row)
            if not isinstance(owner_branch, session_staff.Branch):
                return f"{row.name}: owner branch is unreadable"
            if owner_branch.name == branch:
                return f"{row.name} has a present worktree on this branch"
        if row.kind == "showrunner" and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            doc = resolver.production_doc(row)
            if not isinstance(doc, session_staff.ProductionDoc):
                return f"{row.name}: {doc.reason}"
            if production_field(doc.text.splitlines(), "Merge branch") == branch:
                return f"{row.name} uses this production merge branch"
    return ""


def delete_landed_branch(request: MergeRequest, branch: str, tip: str) -> None:
    """Delete only this request's unowned origin branch after main receives the merge."""
    if isinstance(request.promotion, production_doc.NoPromotion):
        return
    try:
        if reason := branch_keep_reason(request, branch):
            checkpoint_gate.report("branch", "kept", f"origin/{branch}: {reason}")
            return
        checkout = request.production.checkout
        ref = f"refs/remotes/origin/{branch}"
        _ = good(git(checkout, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main",
                     f"+refs/heads/{branch}:{ref}"), "branch")
        if git(checkout, "merge-base", "--is-ancestor", tip, "origin/main").returncode:
            checkpoint_gate.report("branch", "kept", f"origin/{branch}: this merge has not reached main")
            return
        branch_tip = good(git(checkout, "rev-parse", ref), "branch")
        if not branch_is_landed(checkout, branch_tip, "origin/main"):
            checkpoint_gate.report("branch", "kept", f"origin/{branch}: work has not reached main")
            return
        deleted = git(checkout, "push", f"--force-with-lease=refs/heads/{branch}:{branch_tip}",
                      "origin", "--delete", branch)
        if deleted.returncode:
            checkpoint_gate.report("branch", "kept",
                                   f"origin/{branch}: delete failed or branch moved; {failure_detail(deleted)}".replace("\n", "; "))
        else:
            checkpoint_gate.report("branch", "deleted", f"origin/{branch}")
    except (checkpoint_gate.Stop, OSError, ValueError, Refusal, staff.StaffRefused) as error:
        checkpoint_gate.report("branch", "kept", f"origin/{branch}: {error}".replace("\n", "; "))


def change_review_commits(request: MergeRequest) -> list[change_review.Commit]:
    """Check required review lines and count checkpoints and commits without a descendant checkpoint."""
    previous = request.production.merge_branch
    try:
        commits = change_review.unit_commits(request.production.checkout, request.commit, previous,
                                             request.production.merge_branch)
    except change_review.ChangeReviewError as error:
        raise checkpoint_gate.Stop("change review", "failed", str(error), "the showrunner will inspect the failed command") from error
    if refused := change_review.missing(commits):
        raise checkpoint_gate.Stop("change review", "held", "; ".join(refused), change_review.HELD_NEXT_STEP)
    selected = change_review.review_commits(commits)
    checkpoint_gate.report("change review", "ok", f"{len(selected)} unit commit(s) carry their {change_review.TRAILER} lines")
    return commits


def checkpoint_step_record(checkout: Path, merge_head: str, step: str) -> Path:
    """Completion belongs to this merge, not to a session's disposable scratch."""
    path = good(git(checkout, "rev-parse", "--git-path", f"checkpoint-completed/{merge_head}/{step}"), step)
    return checkout / path


def record_publication_changes(production: ProductionDocument, *, report_unchanged: bool = True,
                               record: staff.StaffSnapshot | None = None,
                               known_producer: change_review.KnownMergeProducer | change_review.UnknownProducer = change_review.UNKNOWN_PRODUCER) -> None:
    """Scan published merges after every promotion step succeeds."""
    doc = change_review.list_path(production.doc, record=record)
    if doc is None:
        if report_unchanged:
            checkpoint_gate.report("change review", "ok", "the production doc has no Change review line; no rows written")
        return
    try:
        rows = change_review.list_ready(production.checkout, production.doc, production.merge_branch, record=record,
                                        known_producer=known_producer)
    except (change_review.ChangeReviewError, OSError) as error:
        checkpoint_gate.report("change review", "failed", f"{doc} not written: {error}; retry after the repair")
        return
    if rows or report_unchanged:
        checkpoint_gate.report("change review", "ok", f"{len(rows)} row(s) added to {doc}" + "".join(f"; {row}" for row in rows))


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


def _origin_merge_refs(request: MergeRequest) -> list[str]:
    return list(dict.fromkeys([
        f"origin/{request.production.merge_branch}",
        *change_review.reviewed_merge_refs(request.production.checkout, change_review.registered_productions),
    ]))


def _unit_paths(request: MergeRequest, paths: tuple[str, ...]) -> tuple[str, ...]:
    """Keep paths absent at an origin merge base or changed from every base that contains them."""
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
            present = git(request.production.checkout, "ls-tree", "--name-only", "-r", "-z",
                          base.stdout.strip(), "--", *paths)
            if changed.returncode or present.returncode:
                continue
        except OSError:
            continue
        unchanged = set(present.stdout.split("\0")) - set(changed.stdout.split("\0"))
        own.difference_update(unchanged)
    return tuple(path for path in paths if path in own)


def scope(request: MergeRequest, paths: tuple[str, ...]) -> None:
    staged = good(git(request.production.checkout, "diff", "--cached", "--name-only"), "scope")
    if staged:
        raise checkpoint_gate.Stop("scope", "held", f"staged paths in merge checkout: {staged.replace(chr(10), ', ')}",
                   "the showrunner will clear unrelated staged paths, then rerun the checkpoint")
    own_paths = _unit_paths(request, paths)
    imported = len(paths) - len(own_paths)
    paths = own_paths
    if isinstance(request.kind, ShrinkCommit):
        plan = request.unit.plan.as_posix()
        next_items = str(Path(plan).with_name(Path(plan).stem + "-next.md"))
        unexpected = [path for path in paths if path not in (plan, next_items)]
        if unexpected or plan not in paths:
            raise checkpoint_gate.Stop("scope", "held", f"shrink paths must be {plan} and optional {next_items}; got {', '.join(paths)}",
                       "commit only the plan doc and its next-items file, then resend the shrink")
    elif isinstance(request.kind, AsBuiltCommit):
        plan = request.unit.plan.as_posix()
        unexpected = [path for path in paths if not path.endswith(".md")]
        if unexpected or plan not in paths:
            raise checkpoint_gate.Stop("scope", "held", f"an as-built commit carries only docs and must change or delete {plan}; "
                       + f"got {', '.join(paths)}",
                       "commit only the as-built docs and the plan's deletion, then resend the as-built")
    checkpoint_gate.report("scope", "ok", f"{len(paths)} changed paths"
           + (f"; {imported} came from origin merge branches" if imported else ""))


def conflicts(request: MergeRequest) -> None:
    result = git(request.production.checkout, "merge-tree", "--write-tree", "--name-only",
                 request.production.merge_branch, request.commit)
    if result.returncode:
        if result.returncode == 1:
            raise checkpoint_gate.Stop("conflicts", "held", "conflict: " + failure_detail(result),
                       "merge the merge branch on your unit branch, resolve conflicts, and send a new hash")
        raise checkpoint_gate.Stop("conflicts", "failed", result.stderr.strip() or f"merge-tree exit {result.returncode}",
                   "the showrunner will inspect the merge-tree failure")
    checkpoint_gate.report("conflicts", "ok", "merge tree is clean")


def other_units(request: MergeRequest, paths: tuple[str, ...]) -> None:
    if len(request.units) == 1:
        checkpoint_gate.report("other units", "ok", "no other units")
        return
    for unit in request.units:
        if unit.name == request.unit.name:
            continue
        if isinstance(unit, UnitUnavailable):
            checkpoint_gate.report("other units", "unavailable", f"{unit.name}: {unit.reason}")
            continue
        diff = git(request.production.checkout, "diff", "--name-only",
                   f"{request.production.merge_branch}...{unit.branch}")
        branch_paths: set[str] = set(diff.stdout.splitlines()) if diff.returncode == 0 else set()
        status = git(unit.worktree, "status", "--short")
        worktree_paths = {line[3:] for line in status.stdout.splitlines() if len(line) >= 4}
        overlap = sorted(set(paths) & (branch_paths | worktree_paths))
        checkpoint_gate.report("other units", "ok", f"{unit.name}: {', '.join(overlap) if overlap else 'no overlap'}")


def into(path: PurePath, branch: str, why: str, unblocks: str) -> None:
    print(f"into: {path} ({branch}) — {why}; unblocks: {unblocks}", flush=True)


def merge_subject(request: MergeRequest) -> str:
    kind = (" shrink" if isinstance(request.kind, ShrinkCommit) else " as-built" if isinstance(request.kind, AsBuiltCommit)
            else "")
    return (f"Merge {request.unit.name} phase {request.phase}{kind} "
            f"({request.commit[:7]}) into {request.production.merge_branch}")


def merged_checkpoint(request: MergeRequest) -> str:
    return merge_evidence(request.production, request.commit)


def merge(request: MergeRequest) -> str:
    checkout = request.production.checkout
    staged = good(git(checkout, "diff", "--cached", "--name-only"), "merge")
    if staged:
        raise checkpoint_gate.Stop("merge", "held", f"staged paths in merge checkout: {staged.replace(chr(10), ', ')}",
                   "the showrunner will clear unrelated staged paths, then rerun the checkpoint")
    short = request.commit[:7]
    message = merge_subject(request) + "\n\n" + ("Phase plan shrink." if isinstance(request.kind, ShrinkCommit)
                                                  else "Closing as-built docs." if isinstance(request.kind, AsBuiltCommit)
                                                  else request.delivers)
    if request.trailer:
        message += "\n\n" + "\n".join(request.trailer)
    with tempfile.TemporaryDirectory(prefix="merge-message-") as directory:
        message_path = Path(directory) / f"merge_{short}.msg"
        _ = message_path.write_text(message + "\n", encoding="utf-8")
        result = git(checkout, "merge", "--no-ff", "-q", "-F", str(message_path), request.commit)
    if result.returncode:
        raise checkpoint_gate.Stop("merge", "failed", result.stderr.strip() or "merge failed",
                   "the showrunner will inspect the merge failure")
    head = good(git(checkout, "rev-parse", "HEAD"), "merge")
    change_review.record_merge_producer(change_review.KnownMergeProducer(head, request.unit.staff_id))
    checkpoint_gate.report("merge", "ok", f"merged as {head}")
    return head


def undo_unpushed_merge(production: ProductionDocument, merge_head: str, before_merge_tip: str = "") -> None:
    checkout = production.checkout
    head = good(git(checkout, "rev-parse", "HEAD"), "red")
    if head != merge_head:
        raise checkpoint_gate.Stop("red", "failed", f"HEAD moved from unpushed merge {merge_head}",
                   "the showrunner will inspect the merge branch")
    if git(checkout, "merge-base", "--is-ancestor", head,
           f"origin/{production.merge_branch}").returncode == 0:
        raise checkpoint_gate.Stop("red", "failed", "merge is already pushed; cannot reset it",
                   "the showrunner will inspect the merge branch")
    _ = good(git(checkout, "reset", "--keep", before_merge_tip or f"{merge_head}^1"), "red")


def tests(request: MergeRequest, paths: tuple[str, ...], merge_head: str) -> str:
    if not isinstance(request.kind, CodeCheckpoint):
        checkpoint_gate.report("test", "ok", f"{'shrink' if isinstance(request.kind, ShrinkCommit) else 'as-built'} has no package tests")
        return "no tests"
    checkout = request.production.checkout
    test_tip = good(git(checkout, "rev-parse", "HEAD"), "test")
    before_merge_tip = good(git(checkout, "rev-parse", f"{merge_head}^1"), "test")
    return checkpoint_tests(request.production, paths, request.commit, request.merge_tests,
                            request.flakes, request.scratch, test_tip, request.flaky_tests,
                            before_merge_tip=before_merge_tip)


def checkpoint_tests(production: ProductionDocument, paths: tuple[str, ...], checkpoint: str,
                     merge_tests: tuple[tuple[str, ...], ...], flakes: frozenset[str], scratch: Path,
                     merge_head: str, flaky_tests: frozenset[str], *, before_merge_tip: str = "") -> str:
    scratch.mkdir(parents=True, exist_ok=True)
    log = scratch / f"merge_{checkpoint[:7]}_test.log"
    _ = log.write_text("", encoding="utf-8")
    try:
        return checkpoint_gate.run_checkpoint_test_steps(production, paths, merge_tests, flakes, flaky_tests, log)
    except checkpoint_gate.Stop as error:
        if error.step == "red" and error.state == "failed":
            undo_unpushed_merge(production, merge_head, before_merge_tip)
        raise


def undo_failed_push(production: ProductionDocument, old_origin: str, before_merge_tip: str) -> None:
    checkout = production.checkout
    if git(checkout, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        _ = good(git(checkout, "merge", "--abort"), "push")
    remote = good(git(checkout, "ls-remote", "origin", f"refs/heads/{production.merge_branch}"), "push")
    if remote.split() and remote.split()[0] == old_origin:
        _ = good(git(checkout, "reset", "--keep", before_merge_tip), "push")


def merge_main(production: ProductionDocument) -> None:
    """Integrate main before the checkpoint selects and runs its tests."""
    checkout = production.checkout
    _ = good(git(checkout, "fetch", "origin", "main"), "push")
    main = git(checkout, "rev-parse", "origin/main")
    if main.returncode == 0 and git(checkout, "merge-base", "--is-ancestor", "origin/main", "HEAD").returncode:
        merged = git(checkout, "merge", "--no-ff", "-m",
                     "Merge origin/main into " + production.merge_branch, "origin/main")
        if merged.returncode:
            conflicts = good(git(checkout, "diff", "--name-only", "--diff-filter=U"), "push")
            _ = good(git(checkout, "merge", "--abort"), "push")
            if conflicts:
                raise checkpoint_gate.Stop("push", "held", "origin/main conflicts: " + ", ".join(conflicts.splitlines()),
                           "merge origin/main into the merge branch, resolve the conflict, and run the push again")
            _ = good(merged, "push")


@contextmanager
def push_rollback(production: ProductionDocument, before_merge_tip: str) -> Generator[None]:
    """Restore the original tip if preparation or publication fails before the push."""
    old_origin = good(git(production.checkout, "rev-parse", f"origin/{production.merge_branch}"), "push")
    try:
        yield
    except (checkpoint_gate.Stop, OSError, ValueError):
        if before_merge_tip:
            undo_failed_push(production, old_origin, before_merge_tip)
        raise


def prepare_checkpoint(production: ProductionDocument, merge_head: str) -> tuple[str, ...]:
    """Integrate main and select all paths that need checkpoint tests."""
    checkout = production.checkout
    before_merge_tip = good(git(checkout, "rev-parse", f"{merge_head}^1"), "test")
    with push_rollback(production, before_merge_tip):
        merge_main(production)
    output = good(git(checkout, "diff", "--name-only", before_merge_tip, "HEAD"), "test")
    return tuple(output.splitlines())


def push_git(production: ProductionDocument, may_undo: bool) -> str:
    checkout = production.checkout
    before_merge_tip = good(git(checkout, "rev-parse", "HEAD^1"), "push") if may_undo else ""
    with push_rollback(production, before_merge_tip):
        tip = good(git(checkout, "rev-parse", "HEAD"), "push")
        _ = good(git(checkout, "push", "origin", production.merge_branch), "push")
    checkpoint_gate.report("push", "ok", f"{production.merge_branch} pushed at {tip}")
    end_pushed_worktrees(production)
    return tip


@contextmanager
def tested_main_push(production: ProductionDocument, tested_tip: str,
                     retest: Callable[[tuple[str, ...], str], None], before_merge_tip: str,
                     *, follow_main: bool = True) -> Generator[str]:
    """Hold the main lock through retests, the push, and main promotion."""
    checkout = production.checkout
    with ExitStack() as publication_lock:
        with push_rollback(production, before_merge_tip):
            _ = publication_lock.enter_context(main_push_lock(checkout))
            while follow_main:
                _ = good(git(checkout, "fetch", "origin", "main"), "push")
                if git(checkout, "merge-base", "--is-ancestor", "origin/main", tested_tip).returncode == 0:
                    break
                merge_main(production)
                tip = good(git(checkout, "rev-parse", "HEAD"), "test")
                paths = tuple(good(git(checkout, "diff", "--name-only", tested_tip, tip), "test").splitlines())
                retest(paths, tip)
                tested_tip = tip
        yield tested_tip


def refresh_pushed_tip(production: ProductionDocument, tip: str) -> str:
    """Test new main changes before an interrupted promotion publishes them."""
    lines = production.doc.read_text().splitlines()

    def retest(paths: tuple[str, ...], test_tip: str) -> None:
        _ = checkpoint_tests(production, paths, tip, parse_merge_tests(lines, production.checkout),
                             listed_names(lines, "Known flakes"),
                             Path(os.environ.get("MERGE_CHECKPOINT_SCRATCH") or tempfile.gettempdir()),
                             test_tip, listed_names(lines, "Known flaky tests"), before_merge_tip=tip)

    with tested_main_push(production, tip, retest, tip) as tested_tip:
        return push_git(production, False) if tested_tip != tip else tip


def pushed_state(production: ProductionDocument, checkpoint: str) -> PushedTip | NotYetPushed:
    checkout = production.checkout
    branch = production.merge_branch
    _ = good(git(checkout, "fetch", "origin", branch), "push")
    remote = f"origin/{branch}"
    if git(checkout, "merge-base", "--is-ancestor", checkpoint, remote).returncode:
        return NotYetPushed()
    return PushedTip(good(git(checkout, "rev-parse", remote), "push"))


def push_validate(production: ProductionDocument, fix_commit: str, may_undo: bool, cancel_prior: bool = False) -> str:
    script = checkpoint_gate.tool("validate_and_push.sh", Path.home() / ".claude/scripts/validate_and_push/validate_and_push.sh",
                  "MERGE_CHECKPOINT_VALIDATE_AND_PUSH")
    command = [script, "--quick", "--to", production.merge_branch,
               "--fix-commit", fix_commit]
    if cancel_prior:
        command.append("--cancel-prior")
    before_merge_tip = good(git(production.checkout, "rev-parse", "HEAD^1"), "push") if may_undo else ""
    with push_rollback(production, before_merge_tip):
        result = subprocess.run(command, cwd=production.checkout, capture_output=True, text=True, check=False)
        if result.returncode:
            detail = failure_detail(result)
            raise checkpoint_gate.Stop("push", "failed", detail, "repair the push failure and send a new checkpoint")
    tip = good(git(production.checkout, "rev-parse", "HEAD"), "push")
    checkpoint_gate.report("push", "ok", f"validated and pushed {tip}")
    end_pushed_worktrees(production)
    return tip


def promote_local_checkout(checkout: Path, merge_branch: str, tip: str) -> (
        PromotionAlreadyAtTip | PromotionOriginPushed | PromotionAdvanced | PromotionNotPushed):
    """Fast-forward one main checkout and push any eligible unpushed tip."""
    _ = good(git(checkout, "fetch", "origin", merge_branch), "promote")
    current_branch = good(git(checkout, "branch", "--show-current"), "promote")
    if current_branch != "main":
        raise checkpoint_gate.Stop("promote", "failed", f"{checkout} is on {current_branch}, expected main",
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
    if conflict := merge_gate.promotion_edit_conflict(checkout, tip):
        raise checkpoint_gate.Stop("promote", "failed", conflict,
                   "clear the listed checkout paths and rerun this checkpoint")
    result = git(checkout, "merge", "--ff-only", tip)
    if result.returncode:
        status = git(checkout, "status", "--short").stdout.strip()
        raise checkpoint_gate.Stop("promote", "failed", f"{checkout}: {status or result.stderr.strip()}",
                   "the showrunner will clear the listed checkout paths and rerun this checkpoint")
    unpushed = good(git(checkout, "rev-list", "origin/main..main"), "promote")
    foreign = good(git(checkout, "rev-list", "main", "^origin/main", f"^origin/{merge_branch}"), "promote")
    if foreign:
        return PromotionNotPushed(tip, False)
    if unpushed:
        _ = good(git(checkout, "push", "origin", "main"), "promote")
    return PromotionAdvanced(tip)


def pull_mac_checkout(mac: PurePosixPath, source: Path) -> main_publish.MacPulled | main_publish.MacPullPending:
    settings_read = conf.read('merge_checkpoint')
    try:
        tries = settings_read.whole("MAC_PULL_TRIES")
        retry_seconds = settings_read.number("MAC_PULL_RETRY_SECONDS")
    except ValueError as error:
        raise checkpoint_gate.Stop("promote", "failed", str(error), "repair the Mac pull configuration and rerun this checkpoint") from error
    remote = main_publish.mac_pull_command(mac, source)
    attempt = 0
    while True:
        result = subprocess.run(["ssh", "mac", remote], capture_output=True, text=True, check=False)
        match = re.search(r"(?:^|\n)rc=(\d+)", result.stdout)
        if result.returncode == 0 and match is not None and int(match.group(1)) == 0:
            main_publish.clear_pull_only(mac)
            return main_publish.MacPulled()
        attempt += 1
        if attempt == tries:
            if result.returncode == 255 and match is None:
                main_publish.queue_pull_only(f"mac {mac}", main_publish.first_line(result),
                                             pulls=(main_publish.MacCheckoutPull(mac, source),))
                return main_publish.MacPullPending()
            raise checkpoint_gate.Stop("promote", "failed", f"mac {mac}: {result.stdout.strip()} {result.stderr.strip()}",
                       "the showrunner will repair the Mac pull and rerun this checkpoint")
        time.sleep(retry_seconds)


def send_queued_zone(mac: PurePosixPath) -> None:
    """Send a /zone change queued while the Mac was offline: the pull just done carried its commit."""
    done = subprocess.run([PYTHON, str(ZONE_SCRIPT), "drain", "--pulled", str(mac)],
                          capture_output=True, text=True, check=False)
    for line in done.stdout.splitlines():
        checkpoint_gate.report("promote", "ok", line)


def promotion_label(request: MergeRequest | ChildHandoffRequest) -> str:
    return (f"{request.unit.name} phase {request.phase}" if isinstance(request, MergeRequest)
            else f"{request.child.name} style hand-off")


def promote(request: MergeRequest | ChildHandoffRequest, tip: str, *, local_only: bool = False) -> None:
    promote_targets(request.promotion, request.production.merge_branch, tip,
                    source=request.production.checkout, label=promotion_label(request), local_only=local_only)


def finish_promotion(request: MergeRequest | ChildHandoffRequest) -> None:
    finish_promotion_targets(request.promotion, source=request.production.checkout,
                             label=promotion_label(request))


def promote_targets(promotion: production_doc.NoPromotion | production_doc.PromoteTo,
                    branch: str, tip: str, *, source: Path, label: str, local_only: bool = False) -> None:
    """Install a pushed tip at the production's local and Mac destinations."""
    if isinstance(promotion, production_doc.NoPromotion):
        checkpoint_gate.report("promote", "ok", "no promotion configured")
        return
    for checkout in promotion.checkouts:
        result = promote_local_checkout(checkout, branch, tip)
        already_at = isinstance(result, (PromotionAlreadyAtTip, PromotionOriginPushed)) or (
            isinstance(result, PromotionNotPushed) and result.already_at_tip)
        detail = f"{checkout} {'already at' if already_at else 'main at'} {tip}"
        if isinstance(result, PromotionNotPushed):
            detail += "; main holds commits not on origin/" + branch + " and was not pushed"
        checkpoint_gate.report("promote", "ok", detail)
    if not local_only:
        finish_promotion_targets(promotion, source=source, label=label)


def finish_promotion_targets(promotion: production_doc.NoPromotion | production_doc.PromoteTo,
                             *, source: Path, label: str) -> None:
    """Announce local installs and pull the Mac after the main push lock is released."""
    if isinstance(promotion, production_doc.NoPromotion):
        return
    for checkout in promotion.checkouts:
        into(checkout, "main", f"sessions on {os.uname().nodename} run the installed commands from it",
             f"the live gate of {label}")
    if isinstance(promotion.mac, production_doc.MacCheckout):
        mac = promotion.mac.path
        source = promotion.checkouts[0] if promotion.checkouts else source
        outcome = pull_mac_checkout(mac, source)
        if isinstance(outcome, main_publish.MacPullPending):
            checkpoint_gate.report("promote", "ok", f"mac unreachable, pull pending: {mac}")
            return
        checkpoint_gate.report("promote", "ok", f"mac {mac} main pulled")
        send_queued_zone(mac)
        into(mac, "main", "sessions on mac run the installed commands from it",
             f"the live gate of {label}")


def review_numbers(line: str) -> ReviewTrial:
    pattern = (r"review trial: ux (\d+) findings, code (\d+) findings, review-worker minutes ([\d.]+), "
               r"ux check minutes ([\d.]+), ux repair minutes ([\d.]+)")
    match = re.fullmatch(pattern, line.strip())
    if match is None:
        raise Refusal("--review-trial must read: review trial: ux <N> findings, code <N> findings, "
                      + "review-worker minutes <M>, ux check minutes <U>, ux repair minutes <R>")
    return ReviewTrial(match[1], match[2], match[3], match[4], match[5])


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
        checkpoint_gate.report("record", "ok", "checkpoint already recorded")
        return
    detail = ("review ledger and production log updated" if isinstance(request.kind, CodeCheckpoint)
              else f"{'shrink' if isinstance(request.kind, ShrinkCommit) else 'as-built'} logged without review-ledger row")
    if isinstance(request.kind, CodeCheckpoint):
        numbers = request.review_trial
        if isinstance(numbers, NoReviewTrial):
            raise checkpoint_gate.Stop("record", "failed", "review trial absent", "send the notice's full review trial line")
        merged = good(git(request.production.checkout, "show", "-s", "--format=%cI", merge_head), "record")
        script = checkpoint_gate.tool("review_regime.py", Path.home() / ".claude/scripts/production/review_regime.py",
                      "MERGE_CHECKPOINT_REVIEW_REGIME")
        review_command = [PYTHON, script]
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
            raise checkpoint_gate.Stop("record", "failed", failure_detail(result),
                       "the showrunner will repair the review record and rerun")
        if result.stdout.startswith("kept "):
            detail = result.stdout.strip() + "; " + detail
    request.production.log.parent.mkdir(parents=True, exist_ok=True)
    with request.production.log.open("a", encoding="utf-8") as log:
        suffix = "; promoted" if isinstance(request.promotion, production_doc.PromoteTo) else ""
        _ = log.write(f"- {datetime.now(request.production.zone):%H:%M %Z}: {request.unit.name} phase {request.phase} "
                      + f"({request.commit}) merged as {merge_head}; "
                      + ("gate after push" if request.bump else f"{tests_green} green") + f"; pushed{suffix}\n")
    checkpoint_gate.report("record", "ok", detail)


def end_phase_scratch(request: MergeRequest) -> None:
    """Remove the merged phase's scratch folders; a failure warns and never fails the merge."""
    if not isinstance(request.kind, CodeCheckpoint):
        return
    task = f"phase:{request.unit.name}/{request.phase}"
    try:
        removed = scratch.end(task)
    except (scratch.ScratchError, OSError) as error:
        checkpoint_gate.report("scratch", "warning", f"{task} not removed: {error}")
        return
    if removed:
        checkpoint_gate.report("scratch", "ok", f"removed {len(removed)} folder(s) of {task}: " + ", ".join(map(str, removed)))


def notify_style(checkout: Path, branch: str, tip: str) -> bool:
    try:
        script = checkpoint_gate.tool("style_sync.py", Path(__file__).resolve().parents[1] / "style/style_sync.py",
                      "MERGE_CHECKPOINT_STYLE_SYNC")
        _ = sys.stdout.flush()
        result = subprocess.run(
            [PYTHON, script, "notify", "--repo-dir", str(checkout), "--branch", branch, "--tip", tip],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode or result.stdout.strip() or result.stderr.strip():
            detail = failure_detail(result)
            print("style sync: " + detail.replace("\n", "; "), flush=True)
        return result.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("style sync: " + str(error).replace("\n", "; "), flush=True)
        return False


@contextmanager
def _external_merge_wait(arguments: list[str]) -> Generator[None, None, None]:
    if os.environ.get(PROBE_ENV) == "1":
        yield
        return
    recorder = [PYTHON, str(Path(progress_history.__file__))]
    wait_id = ""
    try:
        started = subprocess.run([*recorder, *arguments], capture_output=True, text=True, check=False)
        wait_id = started.stdout.strip() if started.returncode == 0 else ""
        if not wait_id:
            checkpoint_gate.report("wait", "NotRecorded", started.stderr.strip() or "recorder returned no wait id")
    except OSError as error:
        checkpoint_gate.report("wait", "NotRecorded", str(error))
    try:
        yield
    finally:
        if wait_id:
            try:
                finished = subprocess.run([*recorder, "finish-external-wait", "--wait-id", wait_id],
                                          capture_output=True, text=True, check=False)
                if finished.returncode:
                    checkpoint_gate.report("wait", "NotRecorded", finished.stderr.strip() or "cannot close merge gate wait")
            except OSError as error:
                checkpoint_gate.report("wait", "NotRecorded", str(error))


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


def record_pushed_merges(production: ProductionDocument, merge_head: str, source_checkpoint: str,
                         tip: str, *, phase_id: str, issue: str = "") -> None:
    """Receipts survive promotion failures and include origin/main integration merges."""
    repository = production_field(production.doc.read_text(encoding="utf-8").splitlines(), "Repository")
    project = Path(repository).expanduser().name
    merges = good(git(production.checkout, "rev-list", "--ancestry-path", "--merges", "--reverse",
                      f"{merge_head}..{tip}"), "receipt").splitlines()
    for commit in [merge_head, *merges]:
        if commit != merge_head and git(production.checkout, "merge-base", "--is-ancestor",
                                        f"{commit}^2", "origin/main").returncode != 0:
            break  # A retry can see later units' merges; their own call records them.
        source = source_checkpoint if commit == merge_head else good(
            git(production.checkout, "rev-parse", f"{commit}^2"), "receipt")
        stamp = float(good(git(production.checkout, "show", "-s", "--format=%ct", commit), "receipt"))
        _ = progress_history.write_merge_receipt(
            project=project, phase_id=phase_id, issue=issue, merge_commit=commit,
            source_checkpoint=source, target_branch=production.merge_branch, merged_at=stamp,
        )


def start_gate(request: MergeRequest, paths: tuple[str, ...], merge_head: str, tip: str) -> None:
    """Record and launch at most one gate for this tip, without delivering notices."""
    path = checkpoint_step_record(request.production.checkout, merge_head, "bump-gate")
    resume_delivery = False
    launch_failure = ""
    log = path.with_name("bump-gate.log")
    members = request.staff_record if request.staff_record is not None else staff.snapshot()
    unit = next(row for row in members.find(kind="unit", parent=request.production.owner.id)
                if row.name == request.unit.name)
    record: checkpoint_gate.BumpGateRecord = {
        "tip": tip, "checkpoint": request.commit,
        "production_doc": str(request.production.doc), "name": request.production.name,
        "owner_id": str(request.production.owner.id), "unit_id": str(unit.id),
        "merge_branch": request.production.merge_branch, "checkout": str(request.production.checkout),
        "production_log": str(request.production.log.relative_to(request.production.checkout)),
        "zone": str(request.production.zone), "unit": request.unit.name,
        "delivered_to": [],
        "paths": list(paths), "merge_tests": [list(command) for command in request.merge_tests],
        "flakes": sorted(request.flakes), "flaky_tests": sorted(request.flaky_tests),
        "started": datetime.now(request.production.zone).isoformat(), "pid": 0, "proc_start": "",
        "result": "pending", "detail": "", "log": str(log),
    }
    with checkpoint_gate.record_lock(path):
        if path.exists():
            previous = checkpoint_gate.read_record(path)
            if previous["tip"] == tip:
                if previous["result"] != "pending":
                    record = previous
                    resume_delivery = True
                else:
                    live = sessions.process_liveness(previous["pid"], previous["proc_start"])
                    if isinstance(live, sessions.LiveSessionProcess):
                        checkpoint_gate.report("bump gate", "ok", f"tip {tip} gate still runs as pid {previous['pid']}")
                        return
                    if isinstance(live, sessions.SessionLivenessUnknown):
                        checkpoint_gate.report("bump gate", "held", live.reason)
                        return
        if not resume_delivery:
            checkpoint_gate.save_record(path, record)
            with log.open("a", encoding="utf-8") as output:
                try:
                    process = subprocess.Popen(
                        [PYTHON, str(Path(__file__).with_name("bump_gate.py").resolve()), "--record", str(path)],
                        cwd=request.production.checkout, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                    )
                    record["pid"] = process.pid
                    record["proc_start"] = sessions.process_start(process.pid) or ""
                    checkpoint_gate.save_record(path, record)
                except OSError as error:
                    launch_failure = f"start failed: {error}"
                    record["result"] = "red"
                    record["detail"] = launch_failure
                    checkpoint_gate.save_record(path, record)
    if resume_delivery or launch_failure:
        if launch_failure:
            with Path(record["log"]).open("a", encoding="utf-8") as output:
                _ = output.write(launch_failure + "\n")
        checkpoint_gate.report("bump gate", "failed" if launch_failure else "ok",
               f"{launch_failure or 'tip ' + tip + ' already ' + record['result']}; log {record['log']}")
        return
    checkpoint_gate.report("bump gate", "ok", f"started pid {record['pid']} on pushed tip {tip}; log {record['log']}")


def deliver_gate_result(request: MergeRequest, merge_head: str, tip: str) -> None:
    """Retry completed notices only after the caller releases the main push lock."""
    path = checkpoint_step_record(request.production.checkout, merge_head, "bump-gate")
    if not path.exists():
        return
    record = checkpoint_gate.read_record(path)
    if record["tip"] != tip or record["result"] == "pending":
        return
    with Path(record["log"]).open("a", encoding="utf-8") as output, redirect_stdout(output):
        checkpoint_gate.send_result(path, record)


def run(request: MergeRequest) -> int:
    try:
        if request.staff_record is None:
            request = request._replace(staff_record=staff.snapshot())
        checkpoint = git(request.production.checkout, "rev-parse", "--verify", f"{request.commit}^{{commit}}")
        if checkpoint.returncode:
            checkpoint = git(request.unit.worktree, "rev-parse", "--verify", f"{request.commit}^{{commit}}")
        if checkpoint.returncode:
            raise checkpoint_gate.Stop("ancestry", "failed", f"commit {request.commit} is absent",
                       "push the checkpoint and send its hash again")
        request = request._replace(commit=checkpoint.stdout.strip())
        origin = ancestry(request)
        already = origin.already_merged
        tests_green = ("gate after push" if request.bump else
                       "merge tests" if isinstance(request.kind, CodeCheckpoint) else "no tests")
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
            # The merge branch already contains this checkpoint. Its merge
            # parents retain the original incoming commits even after later merges.
            commits = change_review.merge_commits(request.production.checkout, merge_head)
            # A run killed between its merge and its push left a merge nothing has tested.
            steps = ("scope", "conflicts", "other units", "merge")
            if isinstance(pushed, PushedTip) and not request.bump:
                steps += ("test", "red")
            for step in steps:
                checkpoint_gate.report(step, "ok", "checkpoint already merged; resuming promotion")
            paths = changed_paths(request, f"{merge_head}^1")
        before_merge_tip = good(git(request.production.checkout, "rev-parse", f"{merge_head}^1"), "merge")
        test_tip = good(git(request.production.checkout, "rev-parse", "HEAD"), "test")
        if isinstance(pushed, NotYetPushed) and change_review.list_path(request.production.doc, record=request.staff_record) is not None:
            try:
                _ = change_review.publication_changes(commits, request.production.doc, record=request.staff_record,
                                                       known_producer=request.unit.staff_id)
            except (change_review.ChangeReviewError, OSError) as error:
                undo_unpushed_merge(request.production, test_tip, before_merge_tip)
                raise checkpoint_gate.Stop("change review", "failed", f"{error}: the staff record is read before the push",
                           "The staff record is unreadable; nothing was pushed; repair the staff record and send the checkpoint again.") from error
        if isinstance(pushed, NotYetPushed):
            paths = prepare_checkpoint(request.production, merge_head)
        test_tip = good(git(request.production.checkout, "rev-parse", "HEAD"), "test")

        def retest(paths: tuple[str, ...], test_tip: str) -> None:
            nonlocal tests_green
            if request.bump:
                return
            try:
                if isinstance(request.kind, CodeCheckpoint):
                    with merge_gate_wait(request.unit.worktree, request.phase, request.unit.name, request.commit):
                        if isinstance(pushed, PushedTip):
                            tests_green = checkpoint_tests(request.production, paths, request.commit, request.merge_tests,
                                                           request.flakes, request.scratch, test_tip, request.flaky_tests,
                                                           before_merge_tip=pushed.tip)
                        else:
                            tests_green = tests(request, paths, merge_head)
                else:
                    tests_green = tests(request, paths, merge_head)
            except (OSError, ValueError) as error:
                undo_unpushed_merge(request.production, test_tip,
                                    pushed.tip if isinstance(pushed, PushedTip) else before_merge_tip)
                raise checkpoint_gate.Stop("red", "failed", str(error),
                           "repair the test logging failure and send a new checkpoint") from error

        if request.bump:
            checkpoint_gate.report("test", "ok", "user bump: the gate runs on the pushed tip in the background")
        elif isinstance(pushed, NotYetPushed):
            retest(paths, test_tip)
        promotion_done = checkpoint_step_record(request.production.checkout, merge_head, "promotion")
        tip = ""
        try:
            try:
                with ExitStack() as gate_lock:
                    if request.bump:
                        path = checkpoint_step_record(request.production.checkout, merge_head, "bump-gate")
                        _ = gate_lock.enter_context(checkpoint_gate.record_lock(path))
                    published_tip = pushed.tip if isinstance(pushed, PushedTip) else test_tip
                    rollback_tip = pushed.tip if isinstance(pushed, PushedTip) else before_merge_tip
                    with tested_main_push(request.production, published_tip, retest, rollback_tip,
                                          follow_main=not isinstance(pushed, PushedTip) or not promotion_done.exists()) as tested_tip:
                        if isinstance(pushed, PushedTip):
                            tip = pushed.tip
                            if tested_tip != tip:
                                tip = push_git(request.production, False)
                            checkpoint_gate.report("push", "ok", f"{request.production.merge_branch} already pushed at {tip}")
                            end_pushed_worktrees(request.production)
                        else:
                            with push_rollback(request.production, before_merge_tip if not already else ""):
                                if isinstance(request.push, GitPush):
                                    tip = push_git(request.production, False)
                                else:
                                    tip = push_validate(request.production,
                                                        f"ci({request.production.name}): format fixes after {request.unit.name} phase {request.phase}",
                                                        False, request.cancel_prior)
                        if request.bump:
                            paths = tuple(good(git(request.production.checkout, "diff", "--name-only",
                                                   before_merge_tip, tip), "test").splitlines())
                            start_gate(request, paths, merge_head, tip)
                        gate_lock.close()
                        promote(request, tip, local_only=True)
                        delete_landed_branch(request, origin.branch, tip)
            finally:
                if tip:
                    if request.bump:
                        deliver_gate_result(request, merge_head, tip)
                    into(request.production.checkout, request.production.merge_branch,
                         "the merge branch collects every unit's checkpoints", "the unit’s next checkpoint")
            finish_promotion(request)
        finally:
            if tip:
                record_pushed_merges(request.production, merge_head, request.commit, tip, phase_id=request.phase,
                                     issue=str(request.unit.plan))
                notify_published_style(request.production, merge_head, tip)
        record_publication_changes(request.production, record=request.staff_record,
                                   known_producer=change_review.KnownMergeProducer(merge_head, request.unit.staff_id))
        promotion_done.parent.mkdir(parents=True, exist_ok=True)
        promotion_done.touch()
        record(request, merge_head, tests_green)
        end_phase_scratch(request)
        print(f"send {request.unit.name}: phase {request.phase} merged and pushed as {merge_head}; continue with the next step")
        return 0
    except checkpoint_gate.Stop as error:
        checkpoint_gate.report(error.step, error.state, error.detail.replace("\n", "; "))
        print(f"send {request.unit.name}: {error.message}")
        return checkpoint_gate.TREE_CHANGED_STATUS if error.state == "cancelled" else 1
    except (OSError, ValueError) as error:
        checkpoint_gate.report("run", "failed", str(error).replace("\n", "; "))
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
    script = checkpoint_gate.tool("showrunners.py", Path(__file__).with_name("showrunners.py"), "MERGE_CHECKPOINT_SHOWRUNNERS")
    result = subprocess.run(
        [PYTHON, script, action, *arguments],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"showrunners.py {action} exited {result.returncode}")
    return result.stdout.strip()


def child_request(args: argparse.Namespace) -> ChildHandoffRequest:
    production = read_production(cast(str, args.production))
    name = owner_for(cast(str, args.from_child)).name
    try:
        parent = child_runner("parent", name)
    except Refusal as error:
        raise Refusal(f"{name} is not attached under {production.name}") from error
    root_self = cast(bool, args.root_self)
    if root_self and parent:
        raise Refusal(f"{name} reports to {parent}; --root-self needs a root")
    if not root_self and parent != production.name:
        raise Refusal(f"{name} is not attached under {production.name}")
    child = read_production(name)
    lines = child.doc.read_text().splitlines()
    if production_field(lines, "Kind") != "style":
        raise Refusal(f"{name} is not a style production")
    branch_point = production_field(lines, "Branch point").removeprefix("refs/heads/")
    if branch_point != production.merge_branch.removeprefix("refs/heads/"):
        raise Refusal(f"{name} follows branch point {branch_point}, not {production.merge_branch}")
    checkout = production.checkout
    common = good(git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"), "input")
    if common != good(git(child.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"), "input"):
        raise Refusal(f"{name} is not in the parent's repository")
    checkpoint = good(git(checkout, "rev-parse", "--verify", f"{cast(str, args.checkpoint)}^{{commit}}"), "ancestry")
    if git(child.checkout, "merge-base", "--is-ancestor", checkpoint, child.merge_branch).returncode:
        raise Refusal(f"checkpoint {checkpoint[:7]} is not on {name}'s merge branch {child.merge_branch}")
    already = git(checkout, "merge-base", "--is-ancestor", checkpoint, production.merge_branch).returncode == 0
    parent_lines = production.doc.read_text().splitlines()
    mode = production_doc.optional_field(parent_lines, "Push")
    raw = cell_value(mode.text) if isinstance(mode, production_doc.FieldPresent) else "validate_and_push"
    if raw not in ("git", "validate_and_push"):
        raise Refusal(f"unknown Push mode: {raw}")
    delivery: NotMerged | MergedNotPushed | MergedAndPushed = NotMerged()
    if already:
        merge_head = merge_evidence(production, checkpoint)
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


def merge_evidence(production: ProductionDocument, checkpoint: str) -> str:
    checkout, branch = production.checkout, production.merge_branch
    commits = good(git(checkout, "rev-list", "--reverse", "--first-parent", branch), "merge").splitlines()
    index = bisect.bisect_left(commits, True, key=lambda commit:
                              git(checkout, "merge-base", "--is-ancestor", checkpoint, commit).returncode == 0)
    return commits[index] if index < len(commits) else good(git(checkout, "rev-parse", branch), "merge")


def record_handoff_merge(request: ChildHandoffRequest, merge_head: str) -> None:
    try:
        script = checkpoint_gate.tool("style_ledger.py", Path(__file__).resolve().parents[1] / "style/style_ledger.py",
                      "MERGE_CHECKPOINT_STYLE_LEDGER")
        _ = sys.stdout.flush()
        result = subprocess.run(
            [PYTHON, script, "handoff", "merged", "--repo", str(request.child.checkout),
             "--checkpoint", request.checkpoint, "--merge-commit", merge_head],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode:
            detail = failure_detail(result)
            print("hand-off ledger: " + detail.replace("\n", "; "), flush=True)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("hand-off ledger: " + str(error).replace("\n", "; "), flush=True)


def acknowledge_handoff(request: ChildHandoffRequest, merge_head: str) -> None:
    script = checkpoint_gate.tool("send.py", session_send.SEND_SCRIPT, "MERGE_CHECKPOINT_SEND")
    try:
        address = child_runner("address", request.child.name)
        if not address:
            raise Refusal(f"no address for {request.child.name}")
        result = subprocess.run([
            PYTHON, script, "--to", address, "--from", request.production.name,
            "--text", f"From the showrunner of {request.production.name}: hand-off {request.checkpoint[:7]} merged as {merge_head[:7]}.",
            "--timeout", "10",
        ], capture_output=True, text=True, check=False, timeout=15)
        if result.returncode:
            raise Refusal(f"send.py: {failure_detail(result)}")
    except (Refusal, staff.StaffRefused, OSError, ValueError, subprocess.TimeoutExpired) as error:
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
        promote(request, tip, local_only=True)


def finish_handoff_promotion(request: ChildHandoffRequest, merge_head: str) -> None:
    done = checkpoint_step_record(request.production.checkout, merge_head, "handoff-promotion")
    if isinstance(request.promotion, production_doc.PromoteTo) and not done.exists():
        finish_promotion(request)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.touch()


def notify_published_style(production: ProductionDocument, merge_head: str, tip: str) -> None:
    """Notify style once for each published tip, including failed promotions."""
    done = checkpoint_step_record(production.checkout, merge_head, "style-notice-" + tip)
    if done.exists():
        return
    if notify_style(production.checkout, production.merge_branch, tip):
        done.parent.mkdir(parents=True, exist_ok=True)
        done.touch()


def record_handoff_publication(request: ChildHandoffRequest, merge_head: str, tip: str) -> None:
    record_pushed_merges(request.production, merge_head, request.checkpoint, tip,
                         phase_id="", issue=f"{request.child.name} hand-off")
    notify_published_style(request.production, merge_head, tip)


def record_handoff_completion(request: ChildHandoffRequest, merge_head: str, tip: str) -> None:
    entry = f"{request.child.name} style hand-off ({request.checkpoint}) merged as {merge_head}; pushed at {tip}"
    log = request.production.log
    if log.exists() and entry in log.read_text(encoding="utf-8"):
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as output:
        _ = output.write(f"- {datetime.now(request.production.zone):%H:%M %Z}: {entry}\n")


def run_handoff(request: ChildHandoffRequest) -> int:
    checkout, branch, checkpoint = request.production.checkout, request.production.merge_branch, request.checkpoint
    delivery = request.delivery
    before_merge_tip = ""

    def retest(paths: tuple[str, ...], test_tip: str) -> None:
        try:
            with child_handoff_wait(request.child.name, checkpoint):
                _ = checkpoint_tests(request.production, paths, checkpoint, request.merge_tests,
                                     request.flakes, request.scratch, test_tip, request.flaky_tests,
                                     before_merge_tip=before_merge_tip)
        except (OSError, ValueError) as error:
            undo_unpushed_merge(request.production, test_tip, before_merge_tip)
            raise checkpoint_gate.Stop("red", "failed", str(error), "repair the test logging failure and send a new checkpoint") from error

    if isinstance(delivery, MergedAndPushed):
        print(f"hand-off {checkpoint[:7]} already merged as {delivery.merge_head[:7]}", flush=True)
        pushed = pushed_state(request.production, checkpoint)
        tip = pushed.tip if isinstance(pushed, PushedTip) else delivery.merge_head
        before_merge_tip = tip
        try:
            follow_main = isinstance(request.promotion, production_doc.PromoteTo) and not checkpoint_step_record(
                checkout, delivery.merge_head, "handoff-promotion").exists()
            with tested_main_push(request.production, tip, retest, tip, follow_main=follow_main) as tested_tip:
                if tested_tip != tip:
                    tip = push_git(request.production, False)
                promote_handoff(request, delivery.merge_head, tip)
            finish_handoff_promotion(request, delivery.merge_head)
        finally:
            record_handoff_publication(request, delivery.merge_head, tip)
        record_publication_changes(request.production, report_unchanged=False)
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
        subject = f"Merge {request.child.name} hand-off ({checkpoint[:7]}) into {branch}"
        pending_tests.parent.mkdir(parents=True, exist_ok=True)
        pending_tests.touch()
        _ = good(git(checkout, "merge", "--no-ff", "-m", subject, checkpoint), "merge")
        merge_head = good(git(checkout, "rev-parse", "HEAD"), "merge")
    else:
        merge_head = delivery.merge_head
        if pending_tests.exists():
            paths = merge_paths(checkout, f"{merge_head}^1", checkpoint)
    if isinstance(delivery, NotMerged) or pending_tests.exists():
        paths = prepare_checkpoint(request.production, merge_head)
        test_tip = good(git(checkout, "rev-parse", "HEAD"), "test")
        before_merge_tip = good(git(checkout, "rev-parse", f"{merge_head}^1"), "merge")
        retest(paths, test_tip)
        pending_tests.unlink()
    tip = ""
    try:
        test_tip = good(git(checkout, "rev-parse", "HEAD"), "test")
        before_merge_tip = good(git(checkout, "rev-parse", f"{merge_head}^1"), "push")
        with tested_main_push(request.production, test_tip, retest, before_merge_tip):
            with push_rollback(request.production, before_merge_tip if isinstance(delivery, NotMerged) else ""):
                if isinstance(request.push, GitPush):
                    tip = push_git(request.production, False)
                else:
                    tip = push_validate(request.production,
                                        f"ci({request.production.name}): format fixes after {request.child.name} style hand-off",
                                        False)
            if isinstance(request.promotion, production_doc.NoPromotion):
                promote(request, tip)
            promote_handoff(request, merge_head, tip)
        finish_handoff_promotion(request, merge_head)
    finally:
        if tip:
            record_handoff_publication(request, merge_head, tip)
    record_publication_changes(request.production, report_unchanged=False)
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
    _ = parser.add_argument("--shrink", action="store_true")
    _ = parser.add_argument("--as-built", action="store_true")
    _ = parser.add_argument("--trailer", action="append", default=[])
    _ = parser.add_argument("--review-trial")
    _ = parser.add_argument("--parallel")
    _ = parser.add_argument("--delivers")
    _ = parser.add_argument("--cancel-prior", action="store_true")
    _ = parser.add_argument("--bump", action="store_true",
                            help="only a user's bump: push a code checkpoint before its background gate")
    _ = parser.add_argument("--started", required=True)
    _ = parser.add_argument("--regime", choices=("after", "trial"), default="after")
    _ = parser.add_argument("--holds", type=int, default=0)
    _ = parser.add_argument("--merge-defects", type=int, default=0)
    _ = parser.add_argument("--excluded")
    _ = parser.add_argument("--scratch")
    args = parser.parse_args()
    if cast(str | None, args.from_child):
        try:
            _ = user_bump(args)
            if not cast(str | None, args.checkpoint):
                raise Refusal("--from-child requires --checkpoint")
            if any((cast(str | None, args.unit), cast(str | None, args.phase), cast(str | None, args.hash),
                    cast(bool, args.shrink), cast(bool, args.as_built), cast(bool, args.bump), cast(str | None, args.review_trial),
                    cast(str | None, args.parallel), cast(str | None, args.delivers), cast(bool, args.cancel_prior),
                    cast(list[str], args.trailer),
                    cast(str, args.regime) != "after", cast(int, args.holds), cast(int, args.merge_defects),
                    cast(str | None, args.excluded), cast(str | None, args.scratch))):
                raise Refusal("--from-child is separate from unit checkpoint arguments")
            handoff = child_request(args)
            with merge_lock(handoff.production.checkout, handoff.production.name):
                return run_handoff(handoff)
        except checkpoint_gate.Stop as error:
            if error.state in ("cancelled", "held"):
                checkpoint_gate.report(error.step, error.state, error.detail.replace("\n", "; "))
                print(error.message, flush=True)
                return checkpoint_gate.TREE_CHANGED_STATUS if error.state == "cancelled" else 1
            print("merge_checkpoint: " + " ".join(str(error).splitlines()), flush=True)
            return 1
        except (Refusal, staff.StaffRefused, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print("merge_checkpoint: " + " ".join(str(error).splitlines()), flush=True)
            return 1
    if cast(bool, args.root_self) or cast(str | None, args.checkpoint):
        parser.error("--root-self and --checkpoint require --from-child")
    if not all((cast(str | None, args.unit), cast(str | None, args.phase), cast(str | None, args.hash))):
        parser.error("unit, phase and hash are required for a unit checkpoint")
    try:
        request = request_from(args)
        with merge_lock(request.production.checkout, request.production.name):
            return run(request)
    except staff.StaffUnreadable as error:
        checkpoint_gate.report("change review", "failed", f"{error}: the staff record is read before the push")
        return 1
    except (Refusal, staff.StaffRefused, OSError, ValueError) as error:
        checkpoint_gate.report("input", "failed", str(error))
        print(f"send {cast(str, args.unit)}: correct the checkpoint input and resend it")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
