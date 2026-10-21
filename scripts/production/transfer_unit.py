#!/usr/bin/env python3
"""Plan, apply and verify one unit's move to another production, journaled so a refusal resumes.

Usage: transfer_unit.py plan|apply|verify <from slug> <unit> <to production doc> [--scratch <dir>]

The giving showrunner runs it. The unit keeps its Claude session and conversation, its worktree,
its branch and its unmerged commits. apply stops the unit through the shutdown path, as
rename_unit does, commits its plan's Production header on the unit branch, commits the Units row
into the receiving doc and out of the giving doc (the row is moved, not retired), moves the unit's
state to the receiving slug, points its shutdown record at the receiving production, relaunches
the same session and verifies. The relaunch sets the unit's marks from the receiving doc, and the
relaunched session's first prompt names its new production doc, merge branch and showrunner.

The doc commits are not pushed: each goes out with its showrunner's next merge push, as add_unit's
row commit does. The unit takes the receiving merge branch at its own next merge.

Version one moves a unit between productions of one repository on this machine: a unit on the Mac,
or a receiving production in another repository, is refused, since its worktree could not be kept.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_unit  # noqa: E402
import merge_checkpoint  # noqa: E402
import pane_record  # noqa: E402
import production_units  # noqa: E402
import rename_state  # noqa: E402
import rename_unit  # noqa: E402
import retire_unit  # noqa: E402
import showrunners  # noqa: E402
import stall_watch  # noqa: E402
import tidy_wrapped  # noqa: E402
import unit_lookup  # noqa: E402
import unit_move  # noqa: E402
import unit_names  # noqa: E402
from rename_state import RenameRefused, read_json, write_json, write_text  # noqa: E402
from rename_unit import shutdown_record  # noqa: E402
from unit_move import PathMove, git  # noqa: E402

if TYPE_CHECKING:
    from scripts.shutdown.record import SessionShutdown
else:
    # rename_unit puts the shutdown folder on the path.
    from record import SessionShutdown

POOL = Path(__file__).resolve().parent.parent / "direct" / "review_pool.py"


@dataclass
class UnitTransfer:
    from_slug: str
    to_slug: str
    unit: str
    from_doc: str
    to_doc: str
    from_checkout: str
    to_checkout: str
    from_merge_branch: str
    to_merge_branch: str
    branch: str
    worktree_dir: str
    plan_cell: str
    tip: str
    scratch: str
    step: int = 0
    # Filled as the steps run: the three commits, and the shutdown record the unit relaunches from
    # (empty when the unit was not running).
    plan_commit: str = ""
    to_commit: str = ""
    from_commit: str = ""
    account: str = ""
    session_id: str = ""

    # What rename_unit's stop steps read (rename_unit.StopTarget): a transfer keeps the unit's id.
    @property
    def slug(self) -> str:
        return self.from_slug

    @property
    def old(self) -> str:
        return self.unit

    @property
    def new(self) -> str:
        return self.unit

    @property
    def id_matches(self) -> bool:
        return True

    def worktree(self) -> Path:
        return Path(self.worktree_dir)

    @property
    def plan_message(self) -> str:
        return f"chore({self.branch}): {self.unit} moves to production {self.to_slug}"

    @property
    def to_message(self) -> str:
        return f"chore({self.to_slug}): transfer unit {self.unit} from {self.from_slug}"

    @property
    def from_message(self) -> str:
        return f"chore({self.from_slug}): transfer unit {self.unit} to {self.to_slug}"


TEXT_FIELDS = tuple(field.name for field in fields(UnitTransfer) if field.name != "step")
# The fields a fresh transfer fills; the rest start empty.
REQUIRED = TEXT_FIELDS[:TEXT_FIELDS.index("plan_commit")]


def _row(doc: Path, unit: str) -> dict[str, str] | None:
    rows = [row for row in production_units.unit_table(doc.read_text(encoding="utf-8").splitlines())
            if production_units.cell_value(row.get("Unit", "")) == unit]
    if len(rows) > 1:
        raise RenameRefused(f"{unit}: {len(rows)} Units rows in {doc}")
    return rows[0] if rows else None


def _common_dir(directory: Path) -> Path:
    return Path(git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def _merging(directory: Path) -> bool:
    """A merge is half done in this checkout or worktree."""
    return subprocess.run(["git", "-C", str(directory), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True, check=False).returncode == 0


def foreign_commits(transfer: UnitTransfer) -> int:
    """Commits the unit branch shares with the giving merge branch that the receiving one lacks:
    they would land in the receiving production with the unit's next merge."""
    worktree = transfer.worktree()
    base = git(worktree, "merge-base", transfer.tip, f"origin/{transfer.from_merge_branch}")
    return int(git(worktree, "rev-list", "--count", base, f"^origin/{transfer.to_merge_branch}"))


def _production(slug: str) -> add_unit.Production:
    checked = showrunners.checked_doc(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}")
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise RenameRefused(f"{slug} has no readable update timer: {checked.reason}")
    return add_unit.read_production(checked.path)


def build(from_slug: str, given: str, to_doc: Path, scratch: Path) -> UnitTransfer:
    giving = _production(from_slug)
    receiving = add_unit.read_production(to_doc.expanduser().resolve())
    if receiving.slug == giving.slug:
        raise RenameRefused(f"{given} is already in {giving.slug}")
    try:
        unit = given if _row(giving.doc, given) else unit_names.unit_id(given)
    except unit_names.BadName as error:
        raise RenameRefused(str(error)) from error
    row = _row(giving.doc, unit)
    if row is None:
        raise RenameRefused(f"{unit} is not in the {giving.slug} Units table")
    if unit in rename_unit.RESERVED:
        raise RenameRefused(f"{unit} is known to code and stays in its production")
    if production_units.plan_cell_is_retired(row.get("Plan", "")):
        raise RenameRefused(f"{unit} is retired; its row is history")
    if _row(receiving.doc, unit) is not None:
        raise RenameRefused(f"{unit} is already in the {receiving.slug} Units table")
    branch = production_units.cell_value(row.get("Branch", ""))
    worktree = Path(production_units.cell_value(row.get("Worktree", ""))).expanduser()
    if not (worktree / ".git").is_file():
        raise RenameRefused(f"{unit}: no linked worktree at {worktree} on this machine; a Mac unit is not moved")
    if _common_dir(worktree) != _common_dir(receiving.checkout):
        raise RenameRefused(f"{receiving.slug} is in another repository; version one keeps the worktree, so it "
                            + "moves a unit only between productions of one repository")
    if git(worktree, "branch", "--show-current") != branch:
        raise RenameRefused(f"{worktree} is not on {branch}")
    return UnitTransfer(giving.slug, receiving.slug, unit, str(giving.doc), str(receiving.doc),
                        str(giving.checkout), str(receiving.checkout), giving.merge_branch,
                        receiving.merge_branch, branch, str(worktree), row.get("Plan", ""),
                        git(worktree, "rev-parse", "HEAD"), str(scratch))


def _check_journals(transfer: UnitTransfer) -> None:
    for slug in (transfer.from_slug, transfer.to_slug):
        for path in (rename_state.production_journal(slug), rename_state.unit_journal(slug)):
            if rename_state.unfinished(path):
                raise RenameRefused(f"{slug} has an unfinished rename ({path.name}); finish it first")
    if rename_state.unfinished(journal(transfer.to_slug)):
        raise RenameRefused(f"{transfer.to_slug} has an unfinished transfer; finish it first")


def preflight(transfer: UnitTransfer) -> None:
    """The refusals that hold at any step: a merge half done, or commits the receiving side would gain."""
    for directory in (transfer.worktree(), Path(transfer.from_checkout), Path(transfer.to_checkout)):
        if _merging(directory):
            raise RenameRefused(f"a merge is in progress in {directory}; transfer once it ends")
    if count := foreign_commits(transfer):
        raise RenameRefused(f"{transfer.branch} holds {count} {transfer.from_merge_branch} commit(s) that "
                            + f"{transfer.to_merge_branch} lacks; they would move with the unit")


def _check_doc(checkout: Path, merge_branch: str, doc: Path) -> None:
    if git(checkout, "branch", "--show-current") != merge_branch:
        raise RenameRefused(f"{checkout} is not on {merge_branch}")
    if git(checkout, "status", "--porcelain", "--", str(doc)):
        raise RenameRefused(f"{doc} has uncommitted changes")


def _commit_only(directory: Path, paths: list[Path], message: str) -> str:
    """Commit only `paths`, or adopt the commit an earlier run made before its journal recorded it."""
    if git(directory, "show", "-s", "--format=%s", "HEAD") == message:
        return git(directory, "rev-parse", "HEAD")
    relative = [str(path.relative_to(directory)) for path in paths]
    _ = git(directory, "commit", "--only", "-m", message, "--", *relative)
    return git(directory, "rev-parse", "HEAD")


# The steps. Each takes the transfer and a save callback, and is safe to run again.


def _stop(transfer: UnitTransfer, save: Callable[[], None]) -> None:
    rename_unit.check_stoppable(transfer)
    rename_unit.stop_unit(transfer, save)


def _header(transfer: UnitTransfer) -> str:
    doc = Path(transfer.to_doc).relative_to(Path(transfer.to_checkout))
    return f"> **Production: {transfer.to_slug}** — unit `{transfer.unit}`; production doc `{doc}`\n"


def _plans(transfer: UnitTransfer) -> list[Path]:
    return list(unit_move.plan_paths(transfer.plan_cell, transfer.worktree()))


def _plan_text(transfer: UnitTransfer, text: str) -> str:
    """The plan's `> **Production:**` header names the receiving production; its prose is history."""
    return "".join(_header(transfer) if line.startswith("> **Production") else line
                   for line in text.splitlines(keepends=True))


def _commit_plans(transfer: UnitTransfer, save: Callable[[], None]) -> None:
    """The unit reads its production from its plan's header, so the header moves on the unit branch."""
    worktree = transfer.worktree()
    if not transfer.plan_commit:
        changed: list[Path] = []
        for path in _plans(transfer):
            text = path.read_text(encoding="utf-8")
            if (updated := _plan_text(transfer, text)) == text:
                continue
            if git(worktree, "status", "--porcelain", "--", str(path)):
                raise RenameRefused(f"{path} has uncommitted changes; commit them first")
            write_text(path, updated)
            changed.append(path)
        transfer.plan_commit = (_commit_only(worktree, changed, transfer.plan_message) if changed
                                else git(worktree, "rev-parse", "HEAD"))
        save()
    unit_move.check_tip(worktree, transfer.branch, transfer.plan_commit)
    _ = git(worktree, "push", "origin", f"{transfer.branch}:refs/heads/{transfer.branch}")


def _row_line(row: dict[str, str], headings: list[str]) -> str:
    """The giving row in the receiving doc's own columns; a column it lacks gets a dash."""
    return "| " + " | ".join(row.get(heading) or "—" for heading in headings) + " |"


def _commit_to(transfer: UnitTransfer, save: Callable[[], None]) -> None:
    checkout, doc = Path(transfer.to_checkout), Path(transfer.to_doc)
    if transfer.to_commit:
        return
    if git(checkout, "show", "-s", "--format=%s", "HEAD") != transfer.to_message:
        _check_doc(checkout, transfer.to_merge_branch, doc)
        row = _row(Path(transfer.from_doc), transfer.unit)
        if row is None:
            raise RenameRefused(f"{transfer.unit} has no Units row in {transfer.from_doc} to move")
        lines = doc.read_text(encoding="utf-8").splitlines()
        insert_at, _ = production_units.unit_rows(lines)
        lines.insert(insert_at, _row_line(row, production_units.unit_headings(lines)))
        write_text(doc, "\n".join(lines) + "\n")
    transfer.to_commit = _commit_only(checkout, [doc], transfer.to_message)
    save()


def _commit_from(transfer: UnitTransfer, save: Callable[[], None]) -> None:
    checkout, doc = Path(transfer.from_checkout), Path(transfer.from_doc)
    if transfer.from_commit:
        return
    if git(checkout, "show", "-s", "--format=%s", "HEAD") != transfer.from_message:
        _check_doc(checkout, transfer.from_merge_branch, doc)
        lines = doc.read_text(encoding="utf-8").splitlines(keepends=True)
        start = lines.index("## Units\n")
        end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
        kept = [line for index, line in enumerate(lines) if not (
            start < index < end and line.startswith("|")
            and production_units.cell_value(production_units.row_cells(line)[0]) == transfer.unit)]
        write_text(doc, "".join(kept))
    transfer.from_commit = _commit_only(checkout, [doc], transfer.from_message)
    save()


def _move_state(transfer: UnitTransfer, _save: Callable[[], None]) -> None:
    """The stall-watch stretch moves to the receiving slug; the giving pool's reviewers retire, so
    the receiving pool leases its own; the old pane entry goes, since the relaunch records a new one.
    The phase note follows on the unit's next refresh, and the giving showrunner's report state
    keeps its rows as history."""
    for path in (stall_watch.stretch_path, stall_watch.prompt_path):
        unit_move.move(PathMove(path(transfer.from_slug, transfer.unit), path(transfer.to_slug, transfer.unit)))
    retired = subprocess.run([sys.executable, str(POOL), "retire", "--production", transfer.from_slug,
                              "--unit", transfer.unit], capture_output=True, text=True, check=False)
    if retired.returncode:
        raise RenameRefused(f"review pool: {(retired.stderr or retired.stdout).strip()}")
    _ = pane_record.forget_unit(transfer.unit)


def moved_note(transfer: UnitTransfer) -> str:
    """The line the relaunched unit reads first."""
    address = f"~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunners.py address {transfer.to_slug}"
    return (f"While you were stopped the showrunner moved you to production {transfer.to_slug}: your production "
            + f"doc is now {transfer.to_doc}, your merge branch is {transfer.to_merge_branch}, and your showrunner "
            + f"is the {transfer.to_slug} showrunner, whose address `{address}` prints. Your worktree "
            + f"{transfer.worktree_dir}, your branch {transfer.branch} and its commits stay. Take "
            + f"origin/{transfer.to_merge_branch} at your next merge, and send checkpoints to the new showrunner.")


def _restart_record(transfer: UnitTransfer, save: Callable[[], None]) -> None:
    """Point the stopped unit's shutdown record at the receiving production. The relaunch reads its
    doc, so the unit's marks name the receiving slug."""
    if not transfer.session_id:
        return
    import herdr

    stem = unit_names.stem(transfer.unit)
    given = herdr.generated_session_name(f"{transfer.from_slug}-{stem}")

    def change(saved: SessionShutdown) -> None:
        host = saved["session"]["host"]
        if host["kind"] != "unit" or host["unit"] != transfer.unit or host["production"] not in (
                transfer.from_slug, transfer.to_slug):
            raise RenameRefused(f"shutdown record {transfer.session_id} is not {transfer.unit}'s")
        if saved["progress"]["kind"] not in rename_unit.STOPPED:
            raise RenameRefused(f"{transfer.unit}'s shutdown record reads {saved['progress']['kind']}; settle it first")
        host["production"] = transfer.to_slug
        host["doc"] = transfer.to_doc
        if host["session_name"] == given:
            host["session_name"] = herdr.generated_session_name(f"{transfer.to_slug}-{stem}")
        saved["renamed"] = moved_note(transfer)

    try:
        _ = shutdown_record.update_session(transfer.account, transfer.session_id, change)
    except (shutdown_record.NoLiveRecord, shutdown_record.InvalidRecord, OSError) as error:
        raise RenameRefused(f"{transfer.unit}'s shutdown record: {error}") from error
    save()


def _live(slug: str, unit: str) -> bool:
    found = unit_lookup.marked_units(slug).get(unit)
    return found is not None and isinstance(found.claude, unit_lookup.LiveClaude)


def _relaunch(transfer: UnitTransfer, _save: Callable[[], None]) -> None:
    if not transfer.session_id:
        return
    rename_unit.run_shutdown("restart", "--only", transfer.session_id)
    # Past the wait verify reports the unit as not running.
    deadline = time.monotonic() + rename_unit.LIVE_WAIT_SECONDS
    while not _live(transfer.to_slug, transfer.unit) and time.monotonic() < deadline:
        time.sleep(rename_unit.POLL_SECONDS)


def _leased(slug: str, unit: str) -> list[str]:
    """The reviewers of `slug`'s pool still leased to `unit`."""
    listed = subprocess.run([sys.executable, str(POOL), "status", "--production", slug],
                            capture_output=True, text=True, check=False)
    if listed.returncode:
        return [f"(the review pool is unreadable: {(listed.stderr or listed.stdout).strip()})"]
    return [fields[0] for fields in (line.split("\t") for line in listed.stdout.splitlines())
            if len(fields) >= 4 and fields[2] != "unleased"
            and retire_unit.same_unit(fields[2].rsplit(":", 1)[0], unit)]


def verify_failures(transfer: UnitTransfer) -> list[str]:
    """Every place that names the unit's production names the receiving one."""
    unit, failures = transfer.unit, list[str]()
    if _row(Path(transfer.to_doc), unit) is None:
        failures.append(f"{transfer.to_doc}: no Units row for {unit}")
    if _row(Path(transfer.from_doc), unit) is not None:
        failures.append(f"{transfer.from_doc}: the Units row for {unit} is still there")
    for path in _plans(transfer):
        text = path.read_text(encoding="utf-8")
        if _plan_text(transfer, text) != text:
            failures.append(f"{path}: its Production header does not name {transfer.to_slug}")
    if stall_watch.stretch_path(transfer.from_slug, unit).exists():
        failures.append(f"stall-watch: the stretch is still under {transfer.from_slug}")
    if transfer.session_id:
        saved = shutdown_record.find_session(transfer.account, transfer.session_id)
        if saved is not None and saved["session"]["host"]["kind"] == "unit" and (
                saved["session"]["host"]["production"] != transfer.to_slug):
            failures.append(f"shutdown record {transfer.session_id} does not name {transfer.to_slug}")
        if not _live(transfer.to_slug, unit):
            failures.append(f"{unit} is not running under {transfer.to_slug}; relaunch it with "
                            + f"shutdown.py restart --here --only {transfer.session_id}")
    if unit in unit_lookup.marked_units(transfer.from_slug):
        failures.append(f"a session still carries the marks {transfer.from_slug} {unit}")
    if leased := _leased(transfer.from_slug, unit):
        failures.append(f"review pool {transfer.from_slug}: {', '.join(leased)} still leased to {unit}")
    return failures


def _verify(transfer: UnitTransfer, _save: Callable[[], None]) -> None:
    if failures := verify_failures(transfer):
        raise RenameRefused("verify failed:\n  " + "\n  ".join(failures))


class Step(NamedTuple):
    run: Callable[[UnitTransfer, Callable[[], None]], None]
    describe: Callable[[UnitTransfer], str]
    # Whether the step runs under both merge locks. The steps that wait (stop, relaunch) and
    # verify do not, so a merge gate is never held behind them.
    locked: bool = True


def _describe_stop(t: UnitTransfer) -> str:
    return f"stop {t.unit} and its Codex seats through the shutdown path; its record carries the relaunch"


def _describe_plans(t: UnitTransfer) -> str:
    return f"{t.branch}: commit {t.plan_message!r} (the plan's Production header names {t.to_slug}) and push it"


def _describe_to(t: UnitTransfer) -> str:
    return f"{t.to_merge_branch}: commit {t.to_message!r} (add the Units row)"


def _describe_from(t: UnitTransfer) -> str:
    return f"{t.from_merge_branch}: commit {t.from_message!r} (drop the Units row)"


def _describe_state(t: UnitTransfer) -> str:
    return (f"state: stall-watch stretch {t.from_slug} -> {t.to_slug}; retire the {t.from_slug} reviewers; "
            + "forget the old pane")


def _describe_record(t: UnitTransfer) -> str:
    return (f"shutdown record: production {t.from_slug} -> {t.to_slug} and doc {t.to_doc}; the first prompt "
            + "names the new production")


def _describe_relaunch(t: UnitTransfer) -> str:
    return f"relaunch the same session in {t.worktree_dir} on {t.branch}"


def _describe_verify(t: UnitTransfer) -> str:
    return f"verify: {t.unit} shows under {t.to_slug} and nowhere under {t.from_slug}"


STEPS = (
    Step(_stop, _describe_stop, locked=False),
    Step(_commit_plans, _describe_plans),
    Step(_commit_to, _describe_to),
    Step(_commit_from, _describe_from),
    Step(_move_state, _describe_state),
    Step(_restart_record, _describe_record),
    Step(_relaunch, _describe_relaunch, locked=False),
    Step(_verify, _describe_verify, locked=False),
)


def journal(slug: str) -> Path:
    return tidy_wrapped.owned_paths(slug)[-1] / "transfer_unit.json"


def _load(path: Path) -> UnitTransfer:
    values = unit_move.json_object(read_json(path))
    step = values.get("step")
    if (type(step) is not int or not all(isinstance(values.get(name, ""), str) for name in TEXT_FIELDS)
            or not all(values.get(name) for name in REQUIRED)):
        raise RenameRefused(f"{path}: invalid unit transfer journal")
    return UnitTransfer(*(cast(str, values.get(name, "")) for name in TEXT_FIELDS[:len(REQUIRED)]), step=step,
                        **{name: cast(str, values.get(name, "")) for name in TEXT_FIELDS[len(REQUIRED):]})


def _save(path: Path, transfer: UnitTransfer) -> None:
    write_json(path, {**asdict(transfer), "done": transfer.step == len(STEPS)})


def _resumed(path: Path, unit: str, to_doc: Path) -> UnitTransfer | None:
    """The unfinished transfer the journal holds, which must be this request; None without one."""
    if not rename_state.unfinished(path):
        return None
    recorded = _load(path)
    if recorded.unit not in (unit, unit_names.unit_id(unit)) or Path(recorded.to_doc) != to_doc.expanduser().resolve():
        raise RenameRefused(f"the unfinished transfer is {recorded.unit} to {recorded.to_slug}; resume it first with "
                            + f"transfer_unit.py apply {recorded.from_slug} {recorded.unit} {recorded.to_doc}")
    return recorded


@contextmanager
def _merge_locks(transfer: UnitTransfer) -> Generator[None]:
    """Both productions' merge locks, taken in slug order so two transfers cannot deadlock."""
    pairs = sorted(((transfer.from_slug, transfer.from_checkout), (transfer.to_slug, transfer.to_checkout)))
    with merge_checkpoint.merge_lock(Path(pairs[0][1]), pairs[0][0]):
        with merge_checkpoint.merge_lock(Path(pairs[1][1]), pairs[1][0]):
            yield


def describe(transfer: UnitTransfer) -> list[str]:
    return [f"{number}. {step.describe(transfer)}{'  (done)' if number <= transfer.step else ''}"
            for number, step in enumerate(STEPS, 1)]


def plan(from_slug: str, unit: str, to_doc: Path, scratch: Path) -> UnitTransfer:
    recorded = _resumed(journal(from_slug), unit, to_doc)
    if recorded is not None:
        return recorded
    transfer = build(from_slug, unit, to_doc, scratch)
    _check_journals(transfer)
    preflight(transfer)
    return transfer


def apply(from_slug: str, unit: str, to_doc: Path, scratch: Path) -> UnitTransfer:
    path = journal(from_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with unit_move.production_lock():
        transfer = _resumed(path, unit, to_doc)
        if transfer is None:
            transfer = build(from_slug, unit, to_doc, scratch)
            _check_journals(transfer)
            with _merge_locks(transfer):
                preflight(transfer)
            # The request is saved before anything changes, so every later refusal resumes.
            _save(path, transfer)
        for number in range(transfer.step + 1, len(STEPS) + 1):
            step = STEPS[number - 1]
            if step.locked:
                with _merge_locks(transfer):
                    preflight(transfer)
                    step.run(transfer, lambda: _save(path, transfer))
            else:
                step.run(transfer, lambda: _save(path, transfer))
            transfer.step = number
            _save(path, transfer)
    return transfer


def verify(from_slug: str, unit: str, to_doc: Path) -> UnitTransfer:
    """Rerun the verify step of a transfer that has reached it."""
    path = journal(from_slug)
    if not path.exists():
        raise RenameRefused(f"{from_slug} has no unit transfer to verify")
    recorded = _load(path)
    if recorded.unit not in (unit, unit_names.unit_id(unit)) or Path(recorded.to_doc) != to_doc.expanduser().resolve():
        raise RenameRefused(f"the recorded transfer is {recorded.unit} to {recorded.to_slug}")
    if recorded.step < len(STEPS) - 1:
        raise RenameRefused(f"{recorded.unit} to {recorded.to_slug} is at step {recorded.step}; finish it with apply")
    _verify(recorded, lambda: None)
    recorded.step = len(STEPS)
    _save(path, recorded)
    return recorded


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    _ = parser.add_argument("verb", choices=("plan", "apply", "verify"))
    _ = parser.add_argument("from_slug")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("to_doc", type=Path)
    _ = parser.add_argument("--scratch", type=Path, default=None)
    args = parser.parse_args(argv)
    verb, from_slug = cast(str, args.verb), showrunners.canonical_slug(cast(str, args.from_slug))
    unit, to_doc = cast(str, args.unit), cast(Path, args.to_doc)
    scratch = cast(Path | None, args.scratch) or showrunners.session_state_dir(from_slug)
    try:
        transfer = (plan(from_slug, unit, to_doc, scratch) if verb == "plan" else apply(from_slug, unit, to_doc, scratch)
                    if verb == "apply" else verify(from_slug, unit, to_doc))
    except rename_unit.RenameWaiting as error:
        print(f"transfer_unit: {error}", file=sys.stderr)
        return 2
    except (RenameRefused, production_units.Refusal, unit_names.BadName, OSError, UnicodeError) as error:
        print(f"transfer_unit: refused: {error}", file=sys.stderr)
        return 1
    print("\n".join(describe(transfer)))
    if verb != "plan":
        print(f"{transfer.unit} is now in {transfer.to_slug}" + (
            f", relaunched in {transfer.worktree_dir}" if transfer.session_id
            else f"; it was not running: launch it from {transfer.to_doc}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
