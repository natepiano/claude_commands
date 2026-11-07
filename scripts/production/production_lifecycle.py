#!/usr/bin/env python3
"""Run the restartable git and document steps of a production lifecycle."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
from dataclasses import replace
from datetime import datetime
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, cast


from scripts.production import session_staff, staff, unit_config
from scripts.shutdown import stop_resume
from scripts.lib import unit_runs
from scripts.herdr import worker_panes
from scripts.shutdown.remote import local_machine
from scripts.lib.git_exclude import ExcludeRefused, ensure_excluded, require_untracked_ignored

from scripts.production.store_owner import showrunner_id
from scripts.production import showrunners
from scripts.production import showrunner_tree
from scripts.production import tidy_wrapped
from scripts.production import unit_lookup
from scripts.production.production_doc import Production, Refusal, production_field, read_production
from scripts.production.merge_checkpoint import (
                              PromotionAlreadyAtTip, PromotionOriginPushed, PromotionNotPushed,
                              Stop,
                              git, good, merged_checkpoints, promote_local_checkout,
                              pull_mac_checkout, report)
from scripts.production import production_doc
from scripts.production.update_registration import QuestionStaff, RegistrationFailure, judgment_state

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_failure import failure_detail
from scripts.lib import berth_board

PHASE_TABLE = Path(__file__).resolve().parent.parent / "direct" / "phase_table.py"


class PlanLanded(NamedTuple):
    tip: str


class PlanPending(NamedTuple):
    pass


class ProductionPlanned(NamedTuple):
    pass


class ProductionRunning(NamedTuple):
    pass


class ProductionWrapped(NamedTuple):
    pass


class VerdictMissing(NamedTuple):
    pass


class CommitVerdict(NamedTuple):
    sha: str


class UnitRow(NamedTuple):
    name: str
    member: staff.StaffRow
    worktree: Path
    branch: str


class LifecycleStop(Exception):
    def __init__(self, step: str, state: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step
        self.state: str = state
        self.detail: str = detail


def checked(checkout: Path, step: str, *args: str) -> str:
    return good(git(checkout, *args), step)


def command(step: str, cwd: Path, *args: str) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise LifecycleStop(step, "failed", f"{' '.join(args)}: {failure_detail(result)}")
    return result.stdout.strip()


def doc_lines(production: Production) -> list[str]:
    return production.doc.read_text(encoding="utf-8").splitlines()


def doc_status(lines: list[str]) -> ProductionPlanned | ProductionRunning | ProductionWrapped:
    match = next((re.search(r"^> \*\*Status: PRODUCTION — (planned|running|wrapped)\.\*\*", line)
                  for line in lines if line.startswith("> **Status:")), None)
    if match is None:
        raise LifecycleStop("input", "failed", "production doc lacks a valid status")
    status = match.group(1)
    if status == "planned":
        return ProductionPlanned()
    if status == "running":
        return ProductionRunning()
    return ProductionWrapped()


def units(runner: staff.StaffRow, record: staff.StaffSnapshot) -> tuple[UnitRow, ...]:
    """The runner's unit rows in staff order; a parked retirement names the branch, else git does."""
    resolver = session_staff.RowResolver(record)
    rows = record.find(kind="unit", parent=runner.id)
    permanent = [row.name for row in rows
                 if isinstance(config := resolver.unit_config(row), unit_config.UnitConfig)
                 and isinstance(config.plan, unit_config.PermanentPlan)]
    if permanent:
        raise LifecycleStop("wrap", "held", "; ".join(
            f"permanent child {name}: transfer it before wrapping" for name in permanent))
    parsed: list[UnitRow] = []
    for row in rows:
        name = row.name
        if isinstance(row.worktree, staff.WorktreeUnfilled):
            raise LifecycleStop("retire", "held", f"{name}: worktree is unfilled")
        parked = unit_config.read_parked(row.id, cleanup=False)
        if isinstance(parked, unit_config.UnitConfig) and isinstance(parked.retired, unit_config.RetiredSession):
            branch = parked.retired.branch.value
        else:
            found = resolver.unit_branch(row)
            if not isinstance(found, session_staff.Branch):
                raise LifecycleStop("retire", "held", f"{name}: {found.reason}")
            branch = found.name
        parsed.append(UnitRow(name, row, row.worktree.path, branch))
    return tuple(parsed)


def branch_state(production: Production) -> PlanLanded | PlanPending:
    result = git(production.checkout, "show-ref", "--verify", "--quiet",
                 f"refs/heads/{production.merge_branch}")
    if result.returncode:
        return PlanPending()
    return PlanLanded(checked(production.checkout, "checkout", "rev-parse", production.merge_branch))


def checkout_ready(production: Production) -> PlanLanded | PlanPending:
    state = branch_state(production)
    current = checked(production.checkout, "checkout", "branch", "--show-current")
    if isinstance(state, PlanLanded):
        ready = current == production.merge_branch
    else:
        ready = not checked(production.checkout, "checkout", "status", "--porcelain")
    if not ready:
        raise LifecycleStop("checkout", "held",
                            f"Run /showrunner:produce in a checkout on {production.merge_branch}.")
    report("checkout", "ok", f"{production.checkout} on {current or 'detached HEAD'}")
    return state


def move_session_state(production_name: str, scratch: Path) -> None:
    identity = showrunner_id(production_name)
    copied = showrunners.copy_session_state(identity, scratch)
    report("state-dir", "ok", f"{showrunners.session_state_dir(identity)}"
           + (f"; copied {', '.join(copied)} from {scratch}" if copied else ""))


def load(production: Production, lines: list[str], resume: bool, scratch: Path | None) -> None:
    state = checkout_ready(production)
    report("load", "ok", "production doc read")
    if scratch is not None:
        move_session_state(production.name, scratch)
    if not resume and not isinstance(doc_status(lines), ProductionRunning):
        return
    owed: tuple[str, ...] = ()
    try:
        owed = judgment_state(showrunners.session_state_dir(production.owner.id) / "showrunner_state.json", "load").owed
    except FileNotFoundError:
        pass
    except (RegistrationFailure, OSError, ValueError, UnicodeError) as error:
        print(f"anomaly: {error}", file=sys.stderr)
    report("state", "ok", "; ".join(owed) if owed else "no next list")
    record = staff.snapshot()
    try:
        marked = unit_lookup.labeled_sessions(production.name, record=record)
    except OSError as error:
        raise LifecycleStop("session", "failed", str(error)) from error
    checkpoints = {entry.unit: entry for entry in merged_checkpoints(production, record=record)} if isinstance(state, PlanLanded) else {}
    for row in record.find(kind="unit", parent=production.owner.id):
        name = row.name
        entry = checkpoints.get(name)
        detail = f"phase {entry.phase} ({entry.commit[:7]})" if entry is not None else "no code checkpoint"
        report("last-merged", "ok", f"{name}: {detail}")
        found = marked.get(row.name)
        report("session", "ok", f"{name}: " + (f"{found.place.session} live" if found is not None else "gone"))


def doc_path_in_checkout(production: Production, path: Path) -> str:
    try:
        return path.resolve().relative_to(production.checkout.resolve()).as_posix()
    except ValueError as error:
        raise LifecycleStop("open", "failed", f"{path} is outside {production.checkout}") from error


def open_production(production: Production, lines: list[str]) -> None:
    status = doc_status(lines)
    if isinstance(status, ProductionWrapped):
        raise LifecycleStop("open", "held", "production is already wrapped")
    record = staff.snapshot()
    rows = tuple(record.find(kind="unit", parent=production.owner.id))
    resolver = session_staff.RowResolver(record)
    state = checkout_ready(production)
    if isinstance(state, PlanPending):
        _ = checked(production.checkout, "branch", "switch", "-c", production.merge_branch)
        report("branch", "ok", f"created {production.merge_branch}")
    else:
        report("branch", "ok", f"already at {state.tip}")
    updated = list(lines)
    if isinstance(status, ProductionPlanned):
        updated = [line.replace("**Status: PRODUCTION — planned.**", "**Status: PRODUCTION — running.**")
                   for line in updated]
    if updated != lines:
        write_text_atomic(production.doc, "\n".join(updated) + "\n")
    report("doc", "ok", "running")
    paths = [doc_path_in_checkout(production, production.doc)]
    for row in rows:
        name = row.name
        match resolver.unit_config(row):
            case unit_config.UnitConfig(plan=unit_config.PlanFile(path=relative)):
                plan = production.checkout / relative
                if plan.exists():
                    paths.append(doc_path_in_checkout(production, plan))
                else:
                    report("plan", "ok", f"{name}: no plan yet at {plan}; not committed")
            case unit_config.UnitConfig():
                pass
            case _:
                report("plan", "ok", f"{name}: no .unit.toml yet; no plan committed")
    subject = f"production({production.name}): plans for {len(rows)} units"
    subjects = checked(production.checkout, "commit", "log", "--format=%s", production.merge_branch)
    committed_paths = checked(production.checkout, "commit", "ls-tree", "-r", "--name-only", "HEAD", "--", *paths)
    if subject in subjects.splitlines() and set(paths).issubset(committed_paths.splitlines()):
        report("commit", "ok", "plans already committed")
    else:
        _ = checked(production.checkout, "commit", "add", "--", *paths)
        pending = git(production.checkout, "diff", "--cached", "--quiet")
        if pending.returncode == 1:
            _ = checked(production.checkout, "commit", "commit", "-m", subject)
            report("commit", "ok", "production doc and unit plans committed")
        elif pending.returncode == 0:
            report("commit", "ok", "plans already committed")
        else:
            raise LifecycleStop("commit", "failed", pending.stderr.strip())
    tip = checked(production.checkout, "push", "rev-parse", production.merge_branch)
    remote = git(production.checkout, "ls-remote", "origin", f"refs/heads/{production.merge_branch}")
    if remote.returncode:
        raise LifecycleStop("push", "failed", remote.stderr.strip())
    if not remote.stdout.startswith(tip):
        _ = checked(production.checkout, "push", "push", "-u", "origin", production.merge_branch)
        report("push", "ok", f"{production.merge_branch} at {tip}")
    else:
        report("push", "ok", f"already at {tip}")
    log_name = production.log.relative_to(production.checkout).as_posix()
    try:
        ensure_excluded(production.checkout, log_name)
    except ExcludeRefused as error:
        raise LifecycleStop("log", "failed", str(error)) from error
    if not production.log.exists():
        production.log.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(production.log, f"# Production log — {production.name}\n")
    report("log", "ok", f"excluded and initialized {log_name}")


def ci_steps(production: Production, tip: str, no_ci: bool,
             ci_green: CommitVerdict | VerdictMissing,
             smoke_passed: CommitVerdict | VerdictMissing) -> None:
    if no_ci:
        for step in ("ci", "smoke-launch", "mac-run"):
            report(step, "ok", "skipped: no CI")
        return
    if isinstance(ci_green, VerdictMissing):
        raise LifecycleStop("ci", "held", "wait for a green CI point on the merge tip")
    ci_commit = git(production.checkout, "rev-parse", "--verify", f"{ci_green.sha}^{{commit}}")
    if ci_commit.returncode or ci_commit.stdout.strip() != tip:
        raise LifecycleStop("ci", "held", f"CI verdict {ci_green.sha} resolves to "
                            + f"{ci_commit.stdout.strip() or 'no commit'}, merge tip is {tip}")
    report("ci", "ok", "green CI point confirmed")
    if isinstance(smoke_passed, VerdictMissing):
        raise LifecycleStop("smoke-launch", "held", "record the live smoke verdict on this tip")
    smoke_commit = git(production.checkout, "rev-parse", "--verify", f"{smoke_passed.sha}^{{commit}}")
    if smoke_commit.returncode or smoke_commit.stdout.strip() != tip:
        raise LifecycleStop("smoke-launch", "held", f"smoke verdict {smoke_passed.sha} resolves to "
                            + f"{smoke_commit.stdout.strip() or 'no commit'}, merge tip is {tip}")
    report("smoke-launch", "ok", "live smoke verdict passed")
    report("mac-run", "ok", "Mac run is recorded with the CI point")


def promote_main(production: Production, lines: list[str], no_ci: bool,
                 ci_green: CommitVerdict | VerdictMissing,
                 smoke_passed: CommitVerdict | VerdictMissing,
                 verdict_checked: bool = False) -> str:
    state = checkout_ready(production)
    if isinstance(state, PlanPending):
        raise LifecycleStop("promote", "held", "merge branch does not exist")
    tip = checked(production.checkout, "promote", "rev-parse", "HEAD")
    if not verdict_checked:
        ci_steps(production, tip, no_ci, ci_green, smoke_passed)
    _ = checked(production.checkout, "main", "fetch", "origin", "main")
    if git(production.checkout, "merge-base", "--is-ancestor", "origin/main", tip).returncode:
        raise LifecycleStop("main", "held", f"origin/main has commits absent from {tip}")
    report("main", "ok", f"origin/main is behind {tip}")
    promotion = production_doc.promotion_from_doc(lines)
    if isinstance(promotion, production_doc.NoPromotion) or not promotion.checkouts:
        remote = checked(production.checkout, "promote", "rev-parse", "origin/main")
        if remote != tip:
            _ = checked(production.checkout, "push", "push", "origin", f"{tip}:refs/heads/main")
            report("promote", "ok", f"main pushed to {tip}")
        else:
            report("promote", "ok", f"already at {tip}")
    else:
        for destination in promotion.checkouts:
            result = promote_local_checkout(destination, production.merge_branch, tip)
            if isinstance(result, PromotionNotPushed):
                raise LifecycleStop("promote", "held", f"{destination} has main commits outside the merge branch")
            if isinstance(result, PromotionAlreadyAtTip):
                detail = f"{destination}: already at {tip}"
            elif isinstance(result, PromotionOriginPushed):
                detail = f"{destination}: pushed {tip}"
            else:
                detail = f"{destination}: advanced to {tip}"
            report("promote", "ok", detail)
    if isinstance(promotion, production_doc.PromoteTo) and isinstance(promotion.mac, production_doc.MacCheckout):
        source = promotion.checkouts[0] if promotion.checkouts else production.checkout
        pull_mac_checkout(promotion.mac.path, source)
        report("promote", "ok", f"mac {promotion.mac.path} main pulled")
    return tip


def close_out_items(lines: list[str]) -> tuple[str, ...]:
    if "## Close-out" not in lines:
        return ()
    start = lines.index("## Close-out") + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return tuple(line[2:].strip() for line in lines[start:end] if line.startswith("- "))


def archived_doc(production: Production) -> Path | None:
    """Where a wrapped doc kept in a productions/ folder goes: archive/<repository>/<production_name>/ beside it."""
    if production.doc.parent.name != "productions":
        return None
    common = Path(checked(production.checkout, "wrap", "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return production.doc.parent.parent / "archive" / common.parent.name / production.name / production.doc.name


def wrap_production(name: str) -> Production:
    """Recover the recorded document after an interrupted archive move."""
    record = staff.snapshot()
    owner = showrunners.owner_for(name, record)
    if isinstance(owner.doc, staff.LiveDoc) and not owner.doc.path.exists():
        source = owner.doc.path
        checkout = Path(checked(source.parent, "wrap", "rev-parse", "--show-toplevel"))
        renames = checked(checkout, "wrap", "diff", "--cached", "--name-status",
                          "--diff-filter=R", "--find-renames", "-z").split("\0")
        for before, after in zip(renames[1::3], renames[2::3]):
            if checkout / before == source:
                document = production_doc.read_new_production(checkout / after, owner.name)
                production = document.for_owner(owner)
                if archived_doc(replace(production, doc=source)) == production.doc:
                    return production
    return read_production(owner)


def retire_reservations(worktree: Path) -> None:
    board = berth_board.read_board(worktree)
    if isinstance(board, berth_board.BoardUnavailable):
        report("retire", "skipped", f"board unavailable ({board.reason})")
        return
    for reservation in board.reservations():
        if reservation.holder_worktree_root.resolve() == worktree.resolve():
            _ = command("retire", worktree, "cargo-berth", "release", reservation.reservation_id, "--json")
            report("retire", "ok", f"released reservation {reservation.reservation_id} for {worktree}")


def refuse_running_children(production_name: str, record: staff.StaffSnapshot) -> None:
    """Hold a wrap for running product children; keep style children attached until retirement."""
    try:
        children = showrunner_tree.stored_children(production_name, record=record)
        running: list[str] = []
        discovered = showrunners.registered_showrunners()
        failures = [failure.reason for failure in discovered.lookup_failures if failure.production in children]
        if failures:
            raise OSError("; ".join(failures))
        for child in discovered:
            if child["name"] not in children or not child["socket"]:
                continue
            lines = Path(child["doc"]).read_text(encoding="utf-8").splitlines()
            try:
                is_style = production_field(lines, "Kind") == "style"
            except Refusal:
                is_style = False
            if not is_style:
                running.append(child["name"])
    except (OSError, UnicodeError) as error:
        raise LifecycleStop("children", "failed", str(error)) from error
    if running:
        raise LifecycleStop("children", "held", f"{', '.join(running)} still report{'s' if len(running) == 1 else ''} "
                            + f"to {production_name}; wrap each first, or detach it with `showrunners.py detach <child>`")


def release_path(production_name: str, *, record: staff.StaffSnapshot | None = None) -> Path:
    return showrunners.SHOWRUNNER_STATE_DIR / showrunner_id(production_name, record=record) / "wrap_release.json"


def save_release(production_name: str, released: showrunner_tree.WrapRelease, *,
                 record: staff.StaffSnapshot | None = None) -> None:
    path = release_path(production_name, record=record)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({
        "former_parent": released.former_parent.parent if isinstance(released.former_parent, showrunner_tree.ReportedTo) else None,
        "former_children": released.former_children, "told": released.told,
    }, indent=2, sort_keys=True) + "\n")


def capture_release(production_name: str, *, record: staff.StaffSnapshot | None = None) -> showrunner_tree.WrapRelease:
    path = release_path(production_name, record=record)
    if not path.exists():
        released = showrunner_tree.edges_to_release(production_name, record=record)
        save_release(production_name, released, record=record)
        return released
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a release object")
    fields = cast(dict[str, object], value)
    parent, children, told = (fields.get(key) for key in ("former_parent", "former_children", "told"))
    if (parent is not None and not isinstance(parent, str)) or not isinstance(children, list) or not isinstance(told, list):
        raise ValueError(f"{path}: invalid release capture")
    if not all(isinstance(item, str) for item in [*cast(list[object], children), *cast(list[object], told)]):
        raise ValueError(f"{path}: release production names must be strings")
    return showrunner_tree.WrapRelease(showrunner_tree.ReportedTo(parent) if isinstance(parent, str) else showrunner_tree.Root(),
                                   tuple(cast(list[str], children)), tuple(cast(list[str], told)))


def release_edges(production_name: str, *, record: staff.StaffSnapshot | None = None) -> None:
    """Send captured wrap notices and save each completed delivery."""
    released = capture_release(production_name, record=record)
    notices: dict[str, str] = {}
    if isinstance(released.former_parent, showrunner_tree.ReportedTo):
        notices[released.former_parent.parent] = f"{production_name} wrapped; its recorded parent is retained for history."
    for child in released.former_children:
        notices[child] = f"{production_name} wrapped; {child} keeps its recorded parent for history."
    messages: list[str] = []
    for target, text in notices.items():
        if target not in released.told:
            told = showrunners.tell_others({target: text}, f"showrunner-tree:{production_name if isinstance(released.former_parent, showrunner_tree.ReportedTo) and target == released.former_parent.parent else target}")
            if told.failed:
                raise LifecycleStop("tree", "failed", "; ".join(told.lines))
            released = replace(released, told=(*released.told, target))
            save_release(production_name, released, record=record)
            messages.extend(told.lines)
    above = f"retained parent {released.former_parent.parent}" if isinstance(released.former_parent, showrunner_tree.ReportedTo) else ""
    below = f"retained children {', '.join(released.former_children)}" if released.former_children else ""
    report("tree", "ok", "; ".join(part for part in (above, below, *messages) if part) or "no parent and no child showrunners")


class PreparedWrapUnit(NamedTuple):
    unit: UnitRow
    retirement: unit_config.RetiredSession


def prepare_wrap_units(production: Production, rows: tuple[UnitRow, ...]) -> tuple[PreparedWrapUnit, ...]:
    """Validate all retirement evidence before the first worktree is removed."""
    prepared: list[PreparedWrapUnit] = []
    for unit in rows:
        location = unit.member.worktree
        if isinstance(location, staff.WorktreeUnfilled) or location.machine != local_machine():
            raise LifecycleStop("retire", "held", f"{unit.name}: staff worktree is not on {local_machine()}")
        pair = unit_config.place(unit.member.id, unit.worktree, cleanup=False)
        if isinstance(pair, unit_config.Both) and not pair.same:
            raise LifecycleStop("retire", "held", f"{pair.worktree} and {pair.parked}: configurations differ; no file was changed")
        saved = unit_config.read_parked(unit.member.id, cleanup=False)
        current = unit_config.read(unit.worktree, cleanup=False)
        if isinstance(saved, unit_config.UnitConfig):
            if not isinstance(saved.retired, unit_config.RetiredSession):
                raise LifecycleStop("retire", "held", f"{unit.name}: parked retirement facts are incomplete")
            retirement = saved.retired
        elif isinstance(current, unit_config.ConfigMissing):
            raise LifecycleStop("retire", "held", current.reason)
        elif not unit.member.current_session:
            raise LifecycleStop("retire", "held", f"{unit.name}: the staff row has no director session for retirement")
        else:
            retirement = unit_config.RetiredSession(datetime.now(production.zone).date().isoformat(),
                                                   unit.member.current_session, unit_config.Branch(unit.branch))
        if unit.worktree.exists():
            if isinstance(location, staff.RemovedWorktree):
                raise LifecycleStop("retire", "held", f"{unit.name}: removed worktree path is occupied")
            require_untracked_ignored(unit.worktree, ".unit.toml")
            actual = checked(unit.worktree, "retire", "symbolic-ref", "--quiet", "--short", "HEAD")
            if actual != unit.branch:
                raise LifecycleStop("retire", "held", f"{unit.name}: worktree branch {actual} conflicts with {unit.branch}")
        prepared.append(PreparedWrapUnit(unit, retirement))
    return tuple(prepared)


def retire_wrap_rows(rows: tuple[UnitRow, ...], record: staff.StaffSnapshot, runner: staff.StaffRow) -> bool:
    """Keep worker and parent rows; removal is proved by the wrap's file step."""
    pending: list[staff.StaffId] = []
    for unit in rows:
        if unit.worktree.exists():
            raise LifecycleStop("staff", "held", f"{unit.name}: worktree has not been removed")
        workers = record.find(kind="worker", parent=unit.member.id)
        for worker in workers:
            print(f"kept worker {worker.id} {worker.name} under {unit.name}")
        if workers:
            pending.append(unit.member.id)
    if record.find(kind="worker", parent=runner.id):
        pending.append(runner.id)
    if pending:
        staff.defer_retire(pending)
    return bool(pending)


def tidy_state(production_name: str, document: Path, *, identities: QuestionStaff) -> None:
    """Close owned questions and delete state files. Keep the files when cleanup fails."""
    try:
        removed = tidy_wrapped.tidy(production_name, document, identities=identities)
    except (tidy_wrapped.TidyRefused, OSError) as error:
        report("tidy", "kept", f"{error}; tidy_wrapped.py removes them once the doc says wrapped")
        return
    names = ", ".join(str(path.relative_to(tidy_wrapped.STATE_DIR)) + ("/" if path.parent == tidy_wrapped.STATE_DIR else "")
                      for path in removed)
    report("tidy", "ok", f"removed {names}" if removed else "no state files")


def wrap(production: Production, lines: list[str], no_ci: bool,
         ci_green: CommitVerdict | VerdictMissing,
         smoke_passed: CommitVerdict | VerdictMissing, done: tuple[str, ...]) -> int:
    held = False
    status = doc_status(lines)
    if isinstance(status, ProductionPlanned):
        raise LifecycleStop("wrap", "held", "production must be running")
    record = staff.snapshot()
    if isinstance(status, ProductionRunning):
        refuse_running_children(production.name, record)
    state = checkout_ready(production)
    if isinstance(state, PlanPending):
        raise LifecycleStop("wrap", "held", "merge branch does not exist")
    runner = production.owner
    rows = units(runner, record)
    checkpoints = merged_checkpoints(production, record=record)
    if isinstance(status, ProductionRunning):
        for item in close_out_items(lines):
            if item not in done:
                raise LifecycleStop("close-out", "held", f'{item} is not recorded done; finish it or get the owner\'s OK, then pass --close-out-done "{item}"')
            report("close-out", "ok", f"{item} (recorded done)")
    prepared = prepare_wrap_units(production, rows)
    tip = checked(production.checkout, "wrap", "rev-parse", "HEAD")
    commit_count = "0"
    if isinstance(status, ProductionRunning):
        ci_steps(production, tip, no_ci, ci_green, smoke_passed)
        for unit in rows:
            if unit.worktree.exists():
                worktree_status = checked(unit.worktree, "worktree", "status", "--short")
                if worktree_status:
                    raise LifecycleStop("worktree", "failed", f"{unit.name} at {unit.worktree} is dirty: {worktree_status}")
            local = git(production.checkout, "show-ref", "--verify", "--quiet", f"refs/heads/{unit.branch}")
            remote = git(production.checkout, "ls-remote", "--exit-code", "--heads", "origin", unit.branch)
            if remote.returncode not in (0, 2):
                raise LifecycleStop("worktree", "failed", remote.stderr.strip() or f"ls-remote exited {remote.returncode}")
            if local.returncode == 0:
                branch_ref = unit.branch
            elif remote.returncode == 0:
                branch_ref = f"refs/remotes/origin/{unit.branch}"
                _ = checked(production.checkout, "worktree", "fetch", "origin",
                            f"+refs/heads/{unit.branch}:{branch_ref}")
            else:
                report("worktree", "ok", f"{unit.name} already retired")
                continue
            if git(production.checkout, "merge-base", "--is-ancestor", branch_ref,
                   production.merge_branch).returncode:
                raise LifecycleStop("worktree", "held", f"{unit.name} branch {unit.branch} is not merged")
            report("worktree", "ok", f"{unit.name} clean and merged")
        _ = checked(production.checkout, "push", "push", "origin", production.merge_branch)
        report("push", "ok", f"{production.merge_branch} pushed")
        _ = checked(production.checkout, "main", "fetch", "origin", "main")
        commit_count = checked(production.checkout, "main", "rev-list", "--count", "origin/main..HEAD")
        tip = promote_main(production, lines, no_ci, ci_green, smoke_passed, verdict_checked=True)
    if isinstance(status, ProductionRunning):
        _ = capture_release(production.name, record=record)
    for item in prepared:
        unit = item.unit
        _ = stop_resume.stop_unit_workers(unit.member.id, record)
        remaining = worker_panes.observe(unit.member.id, staff.snapshot()) if record.find(kind="worker", parent=unit.member.id) and unit_runs.worker_transport(worker_panes.worker_run(unit.member)) == "herdr" else []
        if any(isinstance(state, (worker_panes.OpenWorker, worker_panes.UncertainWorker)) for state in remaining):
            held = True
            continue
        _ = unit_config.park(unit.member.id, unit.worktree, item.retirement)
        if unit.worktree.exists():
            retire_reservations(unit.worktree)
            _ = checked(production.checkout, "retire", "worktree", "remove", str(unit.worktree))
            report("retire", "ok", f"removed {unit.name} worktree")
        _ = staff.set_worktree(unit.member.id, staff.RemovedWorktree(local_machine(), unit.worktree),
                               expected=unit.member.worktree)
        if git(production.checkout, "show-ref", "--verify", "--quiet", f"refs/heads/{unit.branch}").returncode == 0:
            _ = checked(production.checkout, "retire", "branch", "-d", unit.branch)
            report("retire", "ok", f"deleted local {unit.branch}")
        remote = git(production.checkout, "ls-remote", "--exit-code", "--heads", "origin", unit.branch)
        if remote.returncode == 0:
            _ = checked(production.checkout, "retire", "push", "origin", "--delete", unit.branch)
            report("retire", "ok", f"deleted origin/{unit.branch}")
        elif remote.returncode != 2:
            raise LifecycleStop("retire", "failed", remote.stderr.strip() or f"ls-remote exited {remote.returncode}")
    held = retire_wrap_rows(rows, record, runner) or held
    if isinstance(status, ProductionRunning):
        release_edges(production.name, record=record)
        archived = command(
            "phase-notes",
            production.checkout,
            sys.executable,
            str(PHASE_TABLE),
            "archive",
            "--production-doc",
            str(production.doc),
        )
        count = len(archived.splitlines())
        report(
            "phase-notes",
            "ok",
            f"archived {count} phase note{'' if count == 1 else 's'}"
            if count
            else "no phase notes to archive",
        )
        notifier = Path.home() / ".claude/scripts/message/notifier.sh"
        _ = command("notifier", production.checkout, str(notifier), "remove", showrunners.timer_name(production.name))
        report("notifier", "ok", "update timer removed")
        updated = [line.replace("**Status: PRODUCTION — running.**", "**Status: PRODUCTION — wrapped.**")
                   for line in lines]
        write_text_atomic(production.doc, "\n".join(updated) + "\n")
    doc_paths = [doc_path_in_checkout(production, production.doc)]
    archive = archived_doc(production)
    if archive is not None:
        archive.parent.mkdir(parents=True, exist_ok=True)
        doc_paths.append(doc_path_in_checkout(production, archive))
        _ = checked(production.checkout, "wrap", "mv", "--", *doc_paths)
        report("wrap", "ok", f"moved the doc out of productions/ to {doc_paths[1]}")
    else:
        renames = checked(production.checkout, "wrap", "diff", "--cached", "--name-status",
                          "--diff-filter=R", "--find-renames", "-z").split("\0")
        doc_paths[:0] = [source for source, destination in zip(renames[1::3], renames[2::3])
                        if destination == doc_paths[-1]]
    final_doc = archive if archive is not None else production.doc
    if not final_doc.is_file():
        raise LifecycleStop("staff", "held", f"wrapped production doc is missing: {final_doc}")
    wrapped_runner = staff.set_doc(runner.id, staff.WrappedDoc(local_machine(), final_doc))
    record = replace(record, rows=tuple(wrapped_runner if row.id == wrapped_runner.id else row for row in record.rows))
    if record.pending_retire:
        _ = staff.retire_pending()
    if git(production.checkout, "diff", "--quiet", "HEAD", "--", *doc_paths).returncode == 1:
        _ = checked(production.checkout, "wrap", "add", "--", doc_paths[-1])
        _ = checked(production.checkout, "wrap", "commit", "--only", "-m",
                    f"production({production.name}): wrapped", "--", *doc_paths)
        report("wrap", "ok", "production doc committed")
    else:
        report("wrap", "ok", "production doc already committed")
    current_tip = checked(production.checkout, "wrap", "rev-parse", "HEAD")
    remote_tip = git(production.checkout, "ls-remote", "origin", f"refs/heads/{production.merge_branch}")
    if remote_tip.returncode:
        raise LifecycleStop("wrap", "failed", remote_tip.stderr.strip())
    if not remote_tip.stdout.startswith(current_tip):
        _ = checked(production.checkout, "wrap", "push", "origin", production.merge_branch)
        report("wrap", "ok", "production doc pushed")
    else:
        report("wrap", "ok", "production doc already pushed")
    if isinstance(status, ProductionWrapped):
        subject = checked(production.checkout, "wrap", "log", "-1", "--format=%s")
        if subject == f"production({production.name}): wrapped":
            tip = checked(production.checkout, "wrap", "rev-parse", "HEAD^")
    print("| Area | Result |\n| --- | --- |")
    for unit in rows:
        merged = [entry for entry in checkpoints if entry.unit == unit.name]
        detail = (f"{unit.name}: {len(merged)} phases merged, last {merged[-1].commit[:7]}" if merged
                  else f"{unit.name}: no phases merged")
        print(f"| Units | {detail} |")
    print(f"| Merge branch | `{production.merge_branch}` at {current_tip[:7]}, pushed |")
    print(f"| CI | {'skipped: no CI' if no_ci else 'green'} |")
    print(f"| Close-out | {', '.join(close_out_items(lines)) or 'none'} |")
    main_result = (f"promoted to {tip[:7]} ({commit_count} commits)" if isinstance(status, ProductionRunning)
                   else f"promoted to {tip[:7]} by the earlier run")
    print(f"| Main | {main_result} |")
    print("| Next | close herdr sessions |")
    if not held:
        tidy_state(production.name, final_doc, identities=QuestionStaff.for_record(record))
    return 1 if held else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("load", "open", "promote-main", "wrap"))
    _ = parser.add_argument("--production", required=True, type=str)
    _ = parser.add_argument("--no-ci", action="store_true")
    _ = parser.add_argument("--resume", action="store_true")
    _ = parser.add_argument("--scratch", type=Path, help="load: the scratchpad whose state moves to the state directory")
    # Accepted and unused: a showrunner started before its name was looked up still passes it.
    _ = parser.add_argument("--session")
    _ = parser.add_argument("--ci-green")
    _ = parser.add_argument("--smoke-passed")
    _ = parser.add_argument("--close-out-done", action="append", default=[])
    args = parser.parse_args()
    try:
        action = cast(str, args.action)
        no_ci = cast(bool, args.no_ci)
        ci_sha = cast(str | None, args.ci_green)
        smoke_sha = cast(str | None, args.smoke_passed)
        if no_ci and (ci_sha is not None or smoke_sha is not None):
            raise LifecycleStop("input", "failed", "--no-ci excludes the verdict flags")
        ci_green = CommitVerdict(ci_sha) if ci_sha is not None else VerdictMissing()
        smoke_passed = CommitVerdict(smoke_sha) if smoke_sha is not None else VerdictMissing()
        name = cast(str, args.production)
        production = wrap_production(name) if action == "wrap" else read_production(name)
        lines = doc_lines(production)
        if action == "load":
            load(production, lines, cast(bool, args.resume), cast(Path | None, args.scratch))
        elif action == "open":
            open_production(production, lines)
        elif action == "promote-main":
            _ = promote_main(production, lines, no_ci, ci_green, smoke_passed)
        else:
            with staff.operation():
                return wrap(production, lines, no_ci, ci_green, smoke_passed,
                 tuple(cast(list[str], args.close_out_done)))
        return 0
    except LifecycleStop as error:
        report(error.step, error.state, error.detail.replace("\n", "; "))
    except Stop as error:
        report(error.step, error.state, error.detail.replace("\n", "; "))
    except (staff.StaffRefused, staff.StaffNotFound, Refusal, OSError, ValueError) as error:
        report("input", "failed", str(error).replace("\n", "; "))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
