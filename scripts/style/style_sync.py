#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Notify standing style productions and record workspace lint coverage."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from scripts.lib import build_folders
from types import FrameType, MappingProxyType
from typing import Literal, cast

SCRIPTS = Path(__file__).resolve().parents[1]
from scripts.lib import process_group

from scripts.style.add_style import NoStyleProduction, standing_style_production
from scripts.production.production_doc import Refusal
from scripts.production import session_staff, staff
from scripts.copies import copies
from scripts.message import send_result, send_budget, session_send
from scripts.production import showrunners
from scripts.style import style_ledger
from scripts.style import style_config
from scripts.style import style_plan
from scripts.style.unit_files import changed_files
from scripts.lib.process_failure import failure_detail
from scripts.lib import scratch

PYTHON = SCRIPTS / "lib/py"
SEND_SECONDS = 10
ROSTER = SCRIPTS / "message/roster.py"
SHOWRUNNERS = SCRIPTS / "production/showrunners.py"
VERIFY = SCRIPTS / "direct/verify.sh"
LEDGER = SCRIPTS / "style/style_ledger.py"
TOOLS: tuple[style_ledger.LintTool, ...] = ("clippy", "mend", "rustfmt")
# Each lint run owns one `style-sync-lint-*` dir here; a later run removes the dir of a run that was killed.
LINT_TEMP_PARENT: Path | build_folders.InheritedTemporaryParent = build_folders.InheritedTemporaryParent()
OWNER_FILE = "owner.json"
# Written by the verifier itself, so a SIGKILL of the owner right after Popen still leaves its group recorded.
VERIFIER_FILE = "verifier.pgid"
# Publishes its group, then runs verify.sh only while the owner lives: a sweep that found the owner dead
# either reads the group (and keeps the dir) or this check stops the wrapper before verify.sh starts.
VERIFIER_WRAPPER = ('printf %s "$$" > "$1.part" && mv "$1.part" "$1" && kill -0 "$2" 2>/dev/null'
                    ' && shift 2 && exec "$@"')
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
# Held everywhere in a run except its work, so no signal lands between the dir, its record and its cleanup.
HELD_SIGNALS = {*STOP_SIGNALS, signal.SIGINT}
CHILD_STOP_SECONDS = 5.0
# send.py's QUEUED (exit 1) keeps the notice for the parent's session, so it arrives later.
HandoffDelivery = Literal["sent", "queued"]


@dataclass(frozen=True)
class ToolLintRecord:
    tool: style_ledger.LintTool
    passed: bool
    diagnostics: Mapping[str, int]


@dataclass(frozen=True)
class ConfiguredStyleProduction:
    name: str
    config: style_config.StyleRepositoryConfig


@dataclass(frozen=True)
class DirectUnitSync:
    """A direct sync uses the parent route and the unit's current branch."""
    route: style_config.ParentUnitRoute
    branch: session_staff.Branch
    unit: staff.StaffId
    worktree: Path


@dataclass(frozen=True)
class NoDirectRoute:
    """The repository has no configured direct style unit."""


def sync_productions(repo: Path) -> tuple[ConfiguredStyleProduction, ...]:
    """Find style configurations in this repository's worktrees."""
    trees = tuple(Path(line.removeprefix("worktree ")) for line in
                  git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree "))
    productions: list[ConfiguredStyleProduction] = []
    for row in staff.snapshot().find(kind="showrunner"):
        if not isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) or not row.doc.path.is_file():
            continue
        if not any(row.doc.path.is_relative_to(tree) for tree in trees):
            continue
        lines = row.doc.path.read_text().splitlines()
        if not style_config.has_style_units(lines):
            continue
        config = style_config.read_config(row.doc.path)
        if isinstance(config.route, style_config.ParentUnitRoute) and not config.assignments:
            continue
        productions.append(ConfiguredStyleProduction(row.name, config))
    return tuple(productions)


def direct_route(repo: Path, unit: str = "") -> DirectUnitSync | NoDirectRoute:
    """Resolve the parent route and the unit branch for a direct sync."""
    productions = tuple(production for production in sync_productions(repo)
                        if isinstance(production.config.route, style_config.ParentUnitRoute))
    if not productions:
        return NoDirectRoute()
    assignments = tuple(assignment for production in productions for assignment in production.config.assignments)
    if not unit:
        if len(assignments) != 1:
            raise Refusal("specify --unit for a repository with multiple direct style units")
        unit = assignments[0].unit
    if unit not in {assignment.unit for assignment in assignments}:
        raise Refusal(f"{unit}: no direct style assignment in this repository")
    record = staff.snapshot()
    route = style_config.config_for_unit(unit, record).route
    assert isinstance(route, style_config.ParentUnitRoute)
    row = next(row for row in record.find(kind="unit") if row.name == unit)
    branch = session_staff.RowResolver(record, rows=(row,)).unit_branch(row)
    if not isinstance(branch, session_staff.Branch):
        raise style_config.StyleConfigError(f"{row.name}: {branch.reason}")
    if not isinstance(row.worktree, staff.PresentWorktree):
        raise style_config.StyleConfigError(f"{row.name}: no unit worktree")
    return DirectUnitSync(route, branch, row.id, row.worktree.path)


def sync_notice(address: str, target: str, branch: str, tip: str) -> None:
    caller = showrunners.own_name() or "style_sync"
    sent = subprocess.run([
        str(PYTHON), str(session_send.SEND_SCRIPT), "--to", address, "--from", caller,
        "--text", f"From {caller}: branch point {branch} moved to {tip[:7]}; sync.",
        "--key", f"style-sync:{target}",
        "--timeout", str(SEND_SECONDS),
    ], capture_output=True, text=True, check=False, timeout=send_budget.seconds(SEND_SECONDS))
    outcome = send_result.send_outcome(sent.returncode, sent.stdout)
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        if outcome.header.outcome in {"sent", "skipped", "kept", "replaced"}:
            return
        if outcome.header.outcome == "retry":
            raise Refusal(f"send.py: needs a retry: {outcome.header.detail}")
    raise Refusal(f"send.py: {failure_detail(sent)}")


def notify(repo_dir: Path, branch: str, tip: str) -> None:
    """A failed notification must never turn a completed merge into a failure."""
    try:
        branch = branch.removeprefix("refs/heads/")
        for production in sync_productions(repo_dir):
            if production.config.route.branch_point.removeprefix("refs/heads/") != branch:
                continue
            if isinstance(production.config.route, style_config.ParentUnitRoute):
                directors = _waiting_directors(production.name)
                targets = tuple(assignment.unit for assignment in production.config.assignments)
            else:
                directors = (WaitingUnitDirector(production.name, runner_value("address", production.name)),)
                targets = (production.name,)
            for target in targets:
                try:
                    sync_notice(director_address(directors, target), target, branch, tip)
                except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
                    if isinstance(production.config.route, style_config.StyleShowrunnerRoute):
                        raise
                    print(f"style_sync: notify {target}: " + " ".join(str(error).splitlines()))
    except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
        detail = " ".join(str(error).splitlines()).removeprefix("style_sync: ")
        print("style_sync: notify: " + detail)


def lint_prefix() -> str:
    """Read the current prefix for style lint folders."""
    return build_folders.FOLDER_PREFIXES["style-sync-lint"]


def lint_temp_parent() -> Path:
    """Read the current temporary parent unless a test supplies its own parent."""
    if isinstance(LINT_TEMP_PARENT, Path):
        return LINT_TEMP_PARENT
    return build_folders.inherited_temporary_parent()


def lint_records(output: str, checkout: Path) -> tuple[ToolLintRecord, ...]:
    """Read verify's workspace tool boundaries, retaining diagnostic file counts."""
    passed = dict.fromkeys(TOOLS, False)
    diagnostics: dict[style_ledger.LintTool, dict[str, int]] = {tool: {} for tool in TOOLS}
    active: style_ledger.LintTool | None = None
    for line in re.sub(r"\x1b\[[0-9;]*m", "", output).splitlines():
        marker = re.fullmatch(r"style-lint: (clippy|mend|rustfmt) (start|passed)", line)
        if marker:
            tool = cast(style_ledger.LintTool, marker[1])
            active = tool if marker[2] == "start" else None
            if marker[2] == "passed":
                passed[tool] = True
            continue
        if active is None:
            continue
        location = re.search(r"(?:-->|:::|Diff in)\s+(.+?):\d+(?::\d+)?", line)
        if location:
            path = Path(location[1])
            if path.is_absolute():
                try:
                    path = path.relative_to(checkout)
                except ValueError:
                    continue
            name = path.as_posix().removeprefix("./")
            diagnostics[active][name] = diagnostics[active].get(name, 0) + 1
    return tuple(ToolLintRecord(tool, passed[tool], MappingProxyType(diagnostics[tool])) for tool in TOOLS)


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def own_handoff(repo: Path, tip: str, style_branch: str, unit: str = "") -> int:
    """Compare the tip with the unit branch or the style branch."""
    tip = git(repo, "rev-parse", "--verify", f"{tip}^{{commit}}")
    sync = direct_route(repo, unit)
    comparison_branch = sync.branch.name if isinstance(sync, DirectUnitSync) else style_branch
    if isinstance(sync, DirectUnitSync):
        style_branch = sync.route.receiving_branch
        with style_ledger.publication_lock(sync.worktree), style_ledger.open_ledger(repo) as ledger:
            _ = style_plan.recover_publication(sync.worktree, ledger)
            opened = ledger.ticket_open(str(sync.unit))
            if any(ticket["state"] == "assigned" for ticket in opened) or changed_files(sync.worktree):
                ledger.wait_for_sync(str(sync.unit))
                return 4
            _ = git(sync.worktree, "merge", "--no-edit", tip)
            for ticket in opened:
                ledger.check_ticket_pipeline(ticket["id"])
            pending = style_ledger.publication_path(sync.worktree, "pending-sync")
            if pending.exists():
                target = pending.read_text().strip()
                if subprocess.run(["git", "-C", str(sync.worktree), "merge-base", "--is-ancestor", target, "HEAD"],
                                  capture_output=True, check=False).returncode == 0:
                    pending.unlink()
            ledger.clear_sync_wait(str(sync.unit))
    style_branch = "refs/heads/" + style_branch.removeprefix("refs/heads/")
    comparison_branch = "refs/heads/" + comparison_branch.removeprefix("refs/heads/")
    tree = git(repo, "rev-parse", "--verify", f"{tip}^{{tree}}")
    if tree != git(repo, "rev-parse", "--verify", f"{comparison_branch}^{{tree}}"):
        return 1
    with style_ledger.open_ledger(repo) as ledger:
        ledger.set_tip(tip, style_branch)
        ledger.tip_done(tip, "own hand-off")
    print("seen (own hand-off: synced with no lint run)")
    return 0


class LintStopped(BaseException):
    """SIGTERM or SIGHUP reached a lint run; like KeyboardInterrupt, no `except Exception` catches it."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"lint stopped by {signal.Signals(signum).name}")
        self.signum: int = signum


@contextmanager
def stop_signals_raise() -> Generator[None]:
    """SIGTERM and SIGHUP raise LintStopped, as SIGINT raises KeyboardInterrupt, so `finally` runs."""
    def stop(signum: int, _frame: FrameType | None) -> None:
        raise LintStopped(signum)

    previous = {signum: signal.signal(signum, stop) for signum in STOP_SIGNALS}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            _ = signal.signal(signum, handler)


@contextmanager
def signal_mask(how: int) -> Generator[None]:
    """Block or unblock HELD_SIGNALS for the enclosed code; a held signal raises once it is unblocked."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    try:
        _ = signal.pthread_sigmask(how, HELD_SIGNALS)
        yield
    finally:
        _ = signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def probe(send: Callable[[int, int], None], target: int) -> bool:
    """Signal 0 only checks; another user's process or an out-of-range id counts as running."""
    try:
        send(target, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError):
        pass
    return True


@dataclass(frozen=True)
class OwnerRecord:
    """Who owns a lint dir, and the git dir its checkout is registered in."""
    owner: int
    repo: Path

    def write(self, directory: Path) -> None:
        """One rename replaces the record, so a sweep never reads half a pid."""
        part = directory / f"{OWNER_FILE}.part"
        _ = part.write_text(json.dumps({"owner": self.owner, "repo": str(self.repo)}))
        _ = part.replace(directory / OWNER_FILE)

    @staticmethod
    def read(directory: Path) -> OwnerRecord:
        payload = cast(object, json.loads((directory / OWNER_FILE).read_text()))
        if not isinstance(payload, dict):
            raise ValueError("owner record is not an object")
        fields = cast(dict[str, object], payload)
        owner, repo = fields.get("owner"), fields.get("repo")
        if not (isinstance(owner, int) and owner > 0 and isinstance(repo, str)):
            raise ValueError("owner record fields are invalid")
        return OwnerRecord(owner, Path(repo))


def lint_run_running(directory: Path, owner_record: OwnerRecord) -> bool:
    """The sweep only probes the verifier group: a reused group id must never get a signal."""
    # Order: owner dead first, then read verifier.pgid, then the caller deletes (see VERIFIER_WRAPPER).
    if probe(os.kill, owner_record.owner):
        return True
    verifier_file = directory / VERIFIER_FILE
    if not verifier_file.exists():
        return False
    group = int(verifier_file.read_text())
    if group <= 0:
        raise ValueError(f"{VERIFIER_FILE} names no process group")
    return probe(process_group.send_process_group, group)


def stop_lint_child(child: subprocess.Popen[str]) -> None:
    """Stop verify.sh and every process it started, so no cargo build writes into a removed checkout."""
    with suppress(ProcessLookupError):
        process_group.send_process_group(child.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        _ = child.wait(CHILD_STOP_SECONDS)
    with suppress(ProcessLookupError):
        process_group.send_process_group(child.pid, signal.SIGKILL)
    _ = child.wait()


def unblock_held_signals() -> None:
    """The child starts with the parent's held mask; preexec_fn is safe here, a lint run starts no threads."""
    _ = signal.pthread_sigmask(signal.SIG_UNBLOCK, HELD_SIGNALS)


def verifier_command(directory: Path, owner: int) -> list[str]:
    return ["sh", "-c", VERIFIER_WRAPPER, "sh", str(directory / VERIFIER_FILE), str(owner),
            "bash", str(VERIFY), "lint", "--check", "--no-cache"]


def verify_lint(directory: Path) -> tuple[int, str]:
    # Held from Popen until the child stop below is in force; start_new_session makes `$$` the group id.
    with signal_mask(signal.SIG_BLOCK), subprocess.Popen(
        verifier_command(directory, os.getpid()), cwd=directory / "checkout", env={**os.environ, "UNIT_DETACHED_RUN": "1",
            "CARGO_BUILD_BUILD_DIR": str((directory / "checkout/target").resolve() / "intermediates")},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True, preexec_fn=unblock_held_signals,
    ) as child:
        try:
            with signal_mask(signal.SIG_UNBLOCK):
                output, _ = child.communicate()
        except BaseException:
            stop_lint_child(child)
            raise
    return child.returncode, output


def remove_lint_dir(repo: Path, directory: Path) -> bool:
    """Delete the checkout, then its registration; the owner record goes last, once nothing is registered.

    git removes the registration of a deleted checkout, also one without its `.git` file, which it
    refuses to validate while the checkout exists. `--force` twice also removes a locked registration,
    which a killed `worktree add` leaves and `git worktree prune` keeps. When either step fails,
    the dir keeps its owner record, so the next sweep retries it. rmtree unlinks checkout/target
    without following it, keeping the repo's warm target.
    """
    snapshot = directory / "checkout"
    # git lists the real path of the dir it was given; only the lint dir is resolved, never the checkout.
    listed = directory.resolve() / "checkout"
    try:
        if snapshot.is_symlink():
            snapshot.unlink()
        elif snapshot.is_dir():
            scratch.remove_with_cargo_output(snapshot, shutil.rmtree)
        registered = {Path(line.removeprefix("worktree "))
                      for line in git(repo, "worktree", "list", "--porcelain").splitlines()
                      if line.startswith("worktree ")}
        if listed in registered:
            _ = git(repo, "worktree", "remove", "--force", "--force", str(listed))
    except (Refusal, OSError) as error:
        print(f"style_sync: kept {directory} for the next lint run; worktree registration: "
              + " ".join(str(error).splitlines()))
        return False
    record = directory / OWNER_FILE
    for entry in directory.iterdir():
        if entry == record:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    record.unlink(missing_ok=True)
    directory.rmdir()
    return True


def remove_if_stale(directory: Path) -> None:
    # A link or another user's entry in a shared temp dir would aim the deletes below at its target.
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.getuid():
        return
    try:
        owner_record = OwnerRecord.read(directory)
        if lint_run_running(directory, owner_record):
            return
    except (OSError, ValueError):
        return
    if remove_lint_dir(owner_record.repo, directory):
        print(f"style_sync: removed stale lint checkout {directory} (owner {owner_record.owner} not running)")


def sweep_stale_lint_dirs() -> None:
    """Remove the dir and registration of each lint run that ended before its cleanup (SIGKILL, power loss).

    A dir without a readable owner record, or whose owner or verifier group runs, stays: a reused id
    keeps a dir, it never removes a running checkout. A failure prints one line and the run continues.
    """
    for directory in sorted(lint_temp_parent().glob(f"{lint_prefix()}*")):
        try:
            remove_if_stale(directory)
        # Any failure of one dir's sweep; LintStopped and KeyboardInterrupt are not an Exception.
        except Exception as error:
            print(f"style_sync: sweep {directory}: " + " ".join(str(error).splitlines()))


def gate_owner(repo: Path) -> str:
    """Use the registered style production, or the calling production."""
    from scripts.production import staff, unit_lookup

    standing = standing_style_production(repo)
    if not isinstance(standing, NoStyleProduction):
        return str(unit_lookup.doc_owner(standing.doc, staff.local_snapshot()).id)
    row = staff.local_snapshot().owner_of(staff.caller_identity())
    return str(row.id) if row is not None else copies.fallback_owner("style")


def lint(repo: Path, checkout: Path, tip: str) -> int:
    """Judge the requested commit, never a style branch with additional fixes."""
    tip = git(checkout, "rev-parse", "--verify", f"{tip}^{{commit}}")
    common_dir = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common_dir != git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"):
        raise Refusal("--repo and --checkout must belong to the same repository")
    sweep_stale_lint_dirs()
    # Signals stay held outside the work, so the dir never exists without its record and its cleanup path.
    with (stop_signals_raise(),
          copies.standing(gate_owner(repo), checkout, copies.CommittedSource(tip)) as snapshot,
          signal_mask(signal.SIG_BLOCK),
          build_folders.temporary_build_folder(build_folders.UniqueBuildFolder(
              lint_prefix(), build_folders.ExplicitBuildParent(lint_temp_parent()))) as directory):
        OwnerRecord(os.getpid(), Path(common_dir)).write(directory)
        with signal_mask(signal.SIG_UNBLOCK):
            (directory / "checkout").symlink_to(snapshot, target_is_directory=True)
            intermediates = snapshot / "target/intermediates"
            retained_target = (snapshot / "target").resolve()
            scratch.mark_cargo_cache(intermediates)
            saved = subprocess.run(
                [str(PYTHON), str(Path(scratch.__file__)), "cargo-save", str(snapshot)],
                env={**os.environ, "CARGO_BUILD_BUILD_DIR": str(intermediates)},
                capture_output=True, text=True, check=False,
            )
            if saved.returncode:
                raise Refusal(saved.stderr.strip() or "cannot save Cargo output roots")
            returncode, output = verify_lint(directory)
            print(output, end="")
            # Build outputs are retained; only source changes invalidate coverage.
            rewritten = bool(git(snapshot, "status", "--porcelain", "--", ".", ":(exclude)target"))
            rewritten = rewritten or (snapshot / "target").resolve() != retained_target
            records = lint_records(output, snapshot)
            for record in records:
                passed = record.passed and not rewritten
                diagnostic_file = directory / f"{record.tool}.json"
                _ = diagnostic_file.write_text(json.dumps(dict(record.diagnostics), sort_keys=True))
                recorded = subprocess.run([
                    str(PYTHON), str(LEDGER), "lint-record", "--repo", str(repo), "--tip", tip,
                    "--tool", record.tool, "--passed", str(int(passed)),
                    "--diagnostics", str(diagnostic_file),
                ], capture_output=True, text=True, check=False)
                if recorded.returncode:
                    raise Refusal(recorded.stderr.strip() or f"lint-record exited {recorded.returncode}")
            if rewritten:
                print("style_sync: lint rewrote the checked tree; no successful coverage recorded")
            return int(returncode != 0 or rewritten
                       or not all(record.passed and not record.diagnostics for record in records))


def runner_value(action: str, name: str) -> str:
    result = subprocess.run(
        [str(PYTHON), str(SHOWRUNNERS), action, name],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"showrunners.py {action} exited {result.returncode}")
    return result.stdout.strip()


def handoff_notice(name: str, parent: str, checkpoint: str) -> HandoffDelivery:
    address = runner_value("address", parent)
    if not address:
        raise Refusal(f"no address for {parent}")
    result = subprocess.run([
        str(PYTHON), str(session_send.SEND_SCRIPT), "--to", address, "--from", name,
        "--text", f"From the showrunner of {name}: hand-off {checkpoint}",
        "--timeout", str(SEND_SECONDS),
    ], capture_output=True, text=True, check=False, timeout=send_budget.seconds(SEND_SECONDS))
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        if outcome.header.outcome in {"queued", "kept"}:
            return "queued"
        if outcome.header.outcome in {"sent", "skipped", "replaced"}:
            return "sent"
    raise Refusal(f"send.py: {failure_detail(result)}")


@dataclass(frozen=True)
class WaitingUnitDirector:
    unit: str
    address: str


def director_address(directors: tuple[WaitingUnitDirector, ...], unit: str) -> str:
    addresses = [director.address for director in directors if director.unit == unit]
    if not addresses:
        raise Refusal("no live director")
    if len(addresses) != 1:
        raise Refusal(f"{len(addresses)} directors")
    return addresses[0]


def _waiting_directors(name: str) -> tuple[WaitingUnitDirector, ...]:
    try:
        result = subprocess.run(
            [str(PYTHON), str(ROSTER), "--json"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if result.returncode not in (0, 1):
            raise Refusal(result.stderr.strip() or f"roster.py exited {result.returncode}")
        payload = cast(object, json.loads(result.stdout))
        if not isinstance(payload, dict):
            raise Refusal("expected an object")
        entries = cast(dict[str, object], payload).get("entries")
        if not isinstance(entries, list):
            raise Refusal("expected entries list")
        directors: list[WaitingUnitDirector] = []
        for entry in cast(list[object], entries):
            if not isinstance(entry, dict):
                raise Refusal("invalid entry")
            fields = cast(dict[str, object], entry)
            if fields.get("role") != "unit director" or fields.get("production") != name:
                continue
            unit, address = fields.get("unit"), fields.get("address")
            if not isinstance(unit, str) or not isinstance(address, str):
                raise Refusal("invalid director unit or address")
            if address:
                directors.append(WaitingUnitDirector(unit, address))
        return tuple(directors)
    except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise Refusal("roster: " + " ".join(str(error).splitlines())) from error


def wake_waiters(repo: Path, ticket_id: str) -> None:
    """Retry waiting directors after a merge, retaining undelivered wakes."""
    with style_ledger.open_ledger(repo) as ledger:
        waits = ledger.sync_waits()
        if not waits:
            return
        try:
            productions = sync_productions(repo)
        except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
            for wait in waits:
                print(f"style_ledger: wake {wait.unit}: " + " ".join(str(error).splitlines()))
            return
        if not productions:
            return
        try:
            directors = tuple(director for production in productions for director in _waiting_directors(production.name)
                              if director.unit in {assignment.unit for assignment in production.config.assignments})
        except Refusal as error:
            for wait in waits:
                print(f"style_ledger: wake {wait.unit}: {error}")
            return
        for wait in waits:
            try:
                unit = ledger.unit_name(wait.unit)
                address = director_address(directors, unit)
                name = next(production.name for production in productions
                            if unit in {assignment.unit for assignment in production.config.assignments})
                result = subprocess.run([
                    str(PYTHON), str(session_send.SEND_SCRIPT), "--to", address, "--from", name,
                    "--text", f"From the showrunner of {name}: ticket {ticket_id} merged; refill.",
                    "--timeout", str(SEND_SECONDS),
                ], capture_output=True, text=True, check=False, timeout=send_budget.seconds(SEND_SECONDS))
                if result.returncode:
                    raise Refusal(f"send.py: {failure_detail(result)}")
            except (Refusal, OSError, ValueError, style_ledger.LedgerError, subprocess.TimeoutExpired) as error:
                print(f"style_ledger: wake {wait.unit}: " + " ".join(str(error).splitlines()))
                continue
            ledger.acknowledge_sync_wait(wait)


def replay_handoffs(ledger: style_ledger.Ledger, name: str, parent: str) -> int:
    failed = False
    for row in ledger.pending_handoffs():
        checkpoint = row["checkpoint"]
        if not parent:
            print(f"root: merge {checkpoint[:7]} with --root-self")
            continue
        try:
            delivery = handoff_notice(name, parent, checkpoint)
        except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print("style_sync: hand-off: " + " ".join(str(error).splitlines()))
            failed = True
            continue
        if delivery == "queued":
            print(f"hand-off {checkpoint[:7]}: queued for {parent}")
        if row["state"] == "pending":
            ledger.handoff_send(checkpoint, parent)
        elif row["sent_to"] != parent:
            ledger.handoff_reroute(checkpoint, parent)
    return int(failed)


def resume(repo: Path, name: str, branch_point: str, style_branch: str) -> int:
    productions = sync_productions(repo)
    production = next((production for production in productions if production.name == name
                       or name in {assignment.unit for assignment in production.config.assignments}), None)
    if production is not None:
        route = production.config.route
        branch_point, style_branch = route.branch_point, route.receiving_branch
    with style_ledger.open_ledger(repo) as ledger:
        for change in ledger.reconcile(branch_point, style_branch):
            print(change, flush=True)
        if production is not None and isinstance(production.config.route, style_config.ParentUnitRoute):
            return 0
        return replay_handoffs(ledger, name, runner_value("parent", name))


def send_handoff(repo: Path, name: str, checkpoint: str) -> int:
    with style_ledger.open_ledger(repo) as ledger:
        checkpoint = git(repo, "rev-parse", "--verify", f"{checkpoint}^{{commit}}")
        ledger.handoff_add(checkpoint)
        parent = runner_value("parent", name)
        if not parent:
            print(f"root: merge {checkpoint[:7]} with --root-self")
            return 0
        try:
            delivery = handoff_notice(name, parent, checkpoint)
        except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print("style_sync: hand-off: " + " ".join(str(error).splitlines()))
            return 1
        if delivery == "queued":
            print(f"hand-off {checkpoint[:7]}: queued for {parent}")
        row = next((row for row in ledger.pending_handoffs() if row["checkpoint"] == checkpoint), None)
        if row is not None:
            if row["state"] == "pending":
                ledger.handoff_send(checkpoint, parent)
            elif row["sent_to"] != parent:
                ledger.handoff_reroute(checkpoint, parent)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    notice = commands.add_parser("notify")
    _ = notice.add_argument("--repo-dir", type=Path, required=True)
    _ = notice.add_argument("--branch", required=True)
    _ = notice.add_argument("--tip", required=True)
    run_lint = commands.add_parser("lint")
    _ = run_lint.add_argument("--repo", type=Path, required=True)
    _ = run_lint.add_argument("--checkout", type=Path, required=True)
    _ = run_lint.add_argument("--tip", required=True)
    own = commands.add_parser("own-handoff")
    _ = own.add_argument("--repo", type=Path, required=True)
    _ = own.add_argument("--tip", required=True)
    _ = own.add_argument("--style-branch", required=True)
    _ = own.add_argument("--unit", default="")
    replay = commands.add_parser("resume")
    handoff = commands.add_parser("send-handoff")
    wake = commands.add_parser("wake-waiters")
    _ = wake.add_argument("--repo", type=Path, required=True)
    _ = wake.add_argument("--ticket", required=True)
    for command in (replay, handoff):
        _ = command.add_argument("--repo", type=Path, required=True)
        _ = command.add_argument("--production", required=True)
    _ = replay.add_argument("--branch-point", required=True)
    _ = replay.add_argument("--style-branch", required=True)
    _ = handoff.add_argument("--checkpoint", required=True)
    args = parser.parse_args(argv)
    if cast(str, args.command) == "notify":
        notify(cast(Path, args.repo_dir), cast(str, args.branch), cast(str, args.tip))
        return 0
    try:
        if cast(str, args.command) == "own-handoff":
            return own_handoff(cast(Path, args.repo), cast(str, args.tip), cast(str, args.style_branch), cast(str, args.unit))
        if cast(str, args.command) == "wake-waiters":
            wake_waiters(cast(Path, args.repo), cast(str, args.ticket))
            return 0
        if cast(str, args.command) == "resume":
            return resume(cast(Path, args.repo), cast(str, args.production),
                          cast(str, args.branch_point), cast(str, args.style_branch))
        if cast(str, args.command) == "send-handoff":
            return send_handoff(cast(Path, args.repo), cast(str, args.production), cast(str, args.checkpoint))
        return lint(cast(Path, args.repo), cast(Path, args.checkout), cast(str, args.tip))
    except LintStopped as stopped:
        print(f"style_sync: {stopped}", file=sys.stderr)
        return 128 + stopped.signum
    except (copies.CopyError, Refusal, style_ledger.LedgerError, scratch.ScratchError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("style_sync: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
