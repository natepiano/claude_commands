#!/usr/bin/env python3
"""Run the restartable git and document steps of a production lifecycle."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, cast

import showrunners
import tidy_wrapped
import unit_lookup
from production_units import Production, read_production
from production_units import Refusal, cell_value, production_field, unit_table
from merge_checkpoint import (CodeCheckpoint, MacCheckout, MergeEntry, NoMerge, NoPromotion,
                              PromotionAlreadyAtTip, PromotionOriginPushed, PromotionNotPushed,
                              PromoteTo, Stop,
                              git, good, merge_branch_history, promote_local_checkout,
                              promotion_from_doc, pull_mac_checkout, report)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import berth_board

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
    plan: Path
    worktree: Path
    branch: str


class RunningStyleChildren(NamedTuple):
    slugs: tuple[str, ...]


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
        raise LifecycleStop(step, "failed", result.stderr.strip() or result.stdout.strip()
                            or f"{' '.join(args)} exited {result.returncode}")
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


def production_name(lines: list[str]) -> str:
    heading = next((line for line in lines if line.startswith("# Production — ")), "")
    if not heading:
        raise LifecycleStop("input", "failed", "production doc lacks its name")
    return heading.removeprefix("# Production — ").strip()


def units(lines: list[str], production: Production) -> tuple[UnitRow, ...]:
    parsed: list[UnitRow] = []
    for cells in unit_table(lines):
        row = {heading: cell_value(cell) for heading, cell in cells.items()}
        if not all(heading in row for heading in ("Unit", "Plan", "Worktree", "Branch", "Port", "Owns")):
            raise LifecycleStop("input", "failed", f"invalid Units row: {cells}")
        # A permanent unit outlives the production: wrap neither waits on it nor retires it.
        if row["Plan"] == "permanent":
            continue
        plan = Path(row["Plan"]).expanduser()
        parsed.append(UnitRow(row["Unit"], plan if plan.is_absolute() else production.checkout / plan,
                              Path(row["Worktree"]).expanduser(), row["Branch"]))
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


def move_session_state(slug: str, scratch: Path) -> None:
    copied = showrunners.copy_session_state(slug, scratch)
    report("state-dir", "ok", f"{showrunners.session_state_dir(slug)}"
           + (f"; copied {', '.join(copied)} from {scratch}" if copied else ""))


def load(production: Production, lines: list[str], resume: bool, scratch: Path | None) -> None:
    state = checkout_ready(production)
    report("load", "ok", "production doc read")
    if scratch is not None:
        move_session_state(production.slug, scratch)
    if not resume and not isinstance(doc_status(lines), ProductionRunning):
        return
    if production.log.exists():
        log_lines = production.log.read_text(encoding="utf-8").splitlines()
        starts = [index for index, line in enumerate(log_lines) if line.startswith("### STATE")]
        state_lines = log_lines[starts[-1]:] if starts else []
        report("state", "ok", " | ".join(state_lines) if state_lines else "no STATE block")
    else:
        report("state", "ok", "no production log yet")
    history = (merge_branch_history(production.checkout, production.merge_branch)
               if isinstance(state, PlanLanded) else None)
    try:
        marked = unit_lookup.marked_units(production.slug)
    except OSError as error:
        raise LifecycleStop("session", "failed", str(error)) from error
    for unit in units(lines, production):
        last: MergeEntry | NoMerge = history.last_code_for_unit(unit.name) if history else NoMerge()
        detail = (f"phase {last.phase} at {last.merge_hash}" if isinstance(last, MergeEntry)
                  else "no code checkpoint")
        report("last-merged", "ok", f"{unit.name}: {detail}")
        found = marked.get(unit.name)
        report("session", "ok", f"{unit.name}: " + (f"{found.place.session} live" if found is not None else "gone"))


def doc_path_in_checkout(production: Production, path: Path) -> str:
    try:
        return path.resolve().relative_to(production.checkout.resolve()).as_posix()
    except ValueError as error:
        raise LifecycleStop("open", "failed", f"{path} is outside {production.checkout}") from error


def open_production(production: Production, lines: list[str]) -> None:
    status = doc_status(lines)
    if isinstance(status, ProductionWrapped):
        raise LifecycleStop("open", "held", "production is already wrapped")
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
        _ = production.doc.write_text("\n".join(updated) + "\n", encoding="utf-8")
    report("doc", "ok", "running")
    rows = units(updated, production)
    paths = [doc_path_in_checkout(production, production.doc)]
    paths.extend(doc_path_in_checkout(production, unit.plan) for unit in rows)
    subject = f"production({production_name(updated)}): plans for {len(rows)} units"
    subjects = checked(production.checkout, "commit", "log", "--format=%s", production.merge_branch)
    if subject in subjects.splitlines():
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
    common = Path(checked(production.checkout, "log", "rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else production.checkout / common
    exclude = common / "info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    log_name = production.log.relative_to(production.checkout).as_posix()
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if log_name not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as stream:
            _ = stream.write(("" if not existing or existing.endswith("\n") else "\n") + log_name + "\n")
    if not production.log.exists():
        production.log.parent.mkdir(parents=True, exist_ok=True)
        _ = production.log.write_text(f"# Production log — {production_name(updated)}\n", encoding="utf-8")
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
    promotion = promotion_from_doc(lines)
    if isinstance(promotion, NoPromotion) or not promotion.checkouts:
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
    if isinstance(promotion, PromoteTo) and isinstance(promotion.mac, MacCheckout):
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
    """Where a wrapped doc kept in a productions/ folder goes: archive/<repository>/<slug>/ beside it."""
    if production.doc.parent.name != "productions":
        return None
    common = Path(checked(production.checkout, "wrap", "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return production.doc.parent.parent / "archive" / common.parent.name / production.slug / production.doc.name


def retire_reservations(worktree: Path) -> None:
    board = berth_board.read_board(worktree)
    if isinstance(board, berth_board.BoardUnavailable):
        report("retire", "skipped", f"board unavailable ({board.reason})")
        return
    for reservation in board.reservations():
        if reservation.holder_worktree_root.resolve() == worktree.resolve():
            _ = command("retire", worktree, "cargo-berth", "release", reservation.reservation_id, "--json")
            report("retire", "ok", f"released reservation {reservation.reservation_id} for {worktree}")


def refuse_running_children(slug: str) -> RunningStyleChildren:
    """Hold a wrap for running product children; keep style children attached until retirement."""
    try:
        children = showrunners.stored_children(slug)
        styles: list[str] = []
        running: list[str] = []
        for child in showrunners.registered_showrunners():
            if child["slug"] not in children or not child["socket"]:
                continue
            lines = Path(child["doc"]).read_text(encoding="utf-8").splitlines()
            try:
                is_style = production_field(lines, "Kind") == "style"
            except Refusal:
                is_style = False
            (styles if is_style else running).append(child["slug"])
    except (OSError, UnicodeError) as error:
        raise LifecycleStop("children", "failed", str(error)) from error
    if running:
        raise LifecycleStop("children", "held", f"{', '.join(running)} still report{'s' if len(running) == 1 else ''} "
                            + f"to {slug}; wrap each first, or detach it with `showrunners.py detach <child>`")
    return RunningStyleChildren(tuple(styles))


def detach_style_children(slug: str, children: RunningStyleChildren) -> None:
    for child in children.slugs:
        try:
            _ = showrunners.detach(child)
        except (OSError, ValueError) as error:
            raise LifecycleStop("children", "failed", str(error)) from error
        text = (f"{slug} wrapped, so you are a root now: run style_sync.py resume and merge your own hand-offs "
                + "(docs/style_production.md, Merging back).")
        told = showrunners.tell_others({child: text}, f"showrunner-tree:{child}")
        report("children", "ok", "; ".join([f"detached {child}; now root", *told]))


def release_edges(slug: str) -> None:
    """Drop the wrapped production's place in the showrunner tree, and tell each production it reported to or had."""
    released = showrunners.release_edges(slug)
    if not released.parent and not released.orphans:
        report("tree", "ok", "no parent and no child showrunners")
        return
    told: list[str] = []
    if released.parent:
        told += showrunners.tell_others({released.parent: f"{slug} wrapped, so it no longer reports to you."},
                                        f"showrunner-tree:{slug}")
    for child in released.orphans:
        told += showrunners.tell_others({child: f"{slug} wrapped, so {child} reports to no one: it is a root now."},
                                        f"showrunner-tree:{child}")
    above = f"detached from {released.parent}" if released.parent else ""
    below = f"{', '.join(released.orphans)} now root{'' if len(released.orphans) == 1 else 's'}" if released.orphans else ""
    report("tree", "ok", "; ".join(part for part in (above, below, *told) if part))


def tidy_state(slug: str) -> None:
    """Delete the production's own footer, pause, open-question and state files; a failure keeps them for the sweep."""
    try:
        removed = tidy_wrapped.tidy(slug)
    except (tidy_wrapped.TidyRefused, OSError) as error:
        report("tidy", "kept", f"{error}; tidy_wrapped.py removes them once the doc says wrapped")
        return
    names = ", ".join(str(path.relative_to(tidy_wrapped.STATE_DIR)) + ("/" if path.parent == tidy_wrapped.STATE_DIR else "")
                      for path in removed)
    report("tidy", "ok", f"removed {names}" if removed else "no state files")


def wrap(production: Production, lines: list[str], no_ci: bool,
         ci_green: CommitVerdict | VerdictMissing,
         smoke_passed: CommitVerdict | VerdictMissing, done: tuple[str, ...]) -> None:
    status = doc_status(lines)
    if isinstance(status, ProductionPlanned):
        raise LifecycleStop("wrap", "held", "production must be running")
    style_children = RunningStyleChildren(())
    if isinstance(status, ProductionRunning):
        style_children = refuse_running_children(production.slug)
    state = checkout_ready(production)
    if isinstance(state, PlanPending):
        raise LifecycleStop("wrap", "held", "merge branch does not exist")
    rows = units(lines, production)
    tip = checked(production.checkout, "wrap", "rev-parse", "HEAD")
    commit_count = "0"
    if isinstance(status, ProductionRunning):
        items = close_out_items(lines)
        for item in items:
            if item not in done:
                raise LifecycleStop("close-out", "held", f'{item} is not recorded done; finish it or get the owner\'s OK, then pass --close-out-done "{item}"')
            report("close-out", "ok", f"{item} (recorded done)")
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
        for unit in rows:
            if unit.worktree.exists():
                retire_reservations(unit.worktree)
                _ = checked(production.checkout, "retire", "worktree", "remove", str(unit.worktree))
                report("retire", "ok", f"removed {unit.name} worktree")
            if git(production.checkout, "show-ref", "--verify", "--quiet", f"refs/heads/{unit.branch}").returncode == 0:
                _ = checked(production.checkout, "retire", "branch", "-d", unit.branch)
                report("retire", "ok", f"deleted local {unit.branch}")
            remote = git(production.checkout, "ls-remote", "--exit-code", "--heads", "origin", unit.branch)
            if remote.returncode == 0:
                _ = checked(production.checkout, "retire", "push", "origin", "--delete", unit.branch)
                report("retire", "ok", f"deleted origin/{unit.branch}")
            elif remote.returncode != 2:
                raise LifecycleStop("retire", "failed", remote.stderr.strip() or f"ls-remote exited {remote.returncode}")
        notifier = Path.home() / ".claude/scripts/message/notifier.sh"
        # The update timer is the one record of the showrunner, so removing it unregisters it.
        _ = command("notifier", production.checkout, str(notifier), "remove", f"showrunner-{production.slug}")
        report("notifier", "ok", "update timer removed")
        detach_style_children(production.slug, style_children)
        release_edges(production.slug)
        tidy_state(production.slug)
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
        updated = [line.replace("**Status: PRODUCTION — running.**", "**Status: PRODUCTION — wrapped.**")
                   for line in lines]
        _ = production.doc.write_text("\n".join(updated) + "\n", encoding="utf-8")
    doc_paths = [doc_path_in_checkout(production, production.doc)]
    archive = archived_doc(production)
    if archive is not None:
        archive.parent.mkdir(parents=True, exist_ok=True)
        doc_paths.append(doc_path_in_checkout(production, archive))
        _ = checked(production.checkout, "wrap", "mv", "--", *doc_paths)
        report("wrap", "ok", f"moved the doc out of productions/ to {doc_paths[1]}")
    if git(production.checkout, "diff", "--quiet", "HEAD", "--", *doc_paths).returncode == 1:
        _ = checked(production.checkout, "wrap", "add", "--", doc_paths[-1])
        _ = checked(production.checkout, "wrap", "commit", "--only", "-m",
                    f"production({production_name(lines)}): wrapped", "--", *doc_paths)
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
        if subject == f"production({production_name(lines)}): wrapped":
            tip = checked(production.checkout, "wrap", "rev-parse", "HEAD^")
    history = merge_branch_history(production.checkout, production.merge_branch)
    print("| Area | Result |\n| --- | --- |")
    for unit in rows:
        merges = [entry for entry in history.entries
                  if isinstance(entry, MergeEntry) and entry.unit == unit.name
                  and isinstance(entry.kind, CodeCheckpoint)]
        last = history.last_code_for_unit(unit.name)
        detail = (f"{unit.name}: {len(merges)} phases merged, last {last.merge_hash[:7]}"
                  if isinstance(last, MergeEntry) else f"{unit.name}: no phases merged")
        print(f"| Units | {detail} |")
    print(f"| Merge branch | `{production.merge_branch}` at {current_tip[:7]}, pushed |")
    print(f"| CI | {'skipped: no CI' if no_ci else 'green'} |")
    print(f"| Close-out | {', '.join(close_out_items(lines)) or 'none'} |")
    main_result = (f"promoted to {tip[:7]} ({commit_count} commits)" if isinstance(status, ProductionRunning)
                   else f"promoted to {tip[:7]} by the earlier run")
    print(f"| Main | {main_result} |")
    print("| Next | close herdr sessions |")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("load", "open", "promote-main", "wrap"))
    _ = parser.add_argument("--production", required=True, type=Path)
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
        production = read_production(cast(Path, args.production))
        lines = doc_lines(production)
        if action == "load":
            load(production, lines, cast(bool, args.resume), cast(Path | None, args.scratch))
        elif action == "open":
            open_production(production, lines)
        elif action == "promote-main":
            _ = promote_main(production, lines, no_ci, ci_green, smoke_passed)
        else:
            wrap(production, lines, no_ci, ci_green, smoke_passed,
                 tuple(cast(list[str], args.close_out_done)))
        return 0
    except LifecycleStop as error:
        report(error.step, error.state, error.detail.replace("\n", "; "))
    except Stop as error:
        report(error.step, error.state, error.detail.replace("\n", "; "))
    except (Refusal, OSError, ValueError) as error:
        report("input", "failed", str(error).replace("\n", "; "))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
