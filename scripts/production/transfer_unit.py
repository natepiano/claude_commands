#!/usr/bin/env python3
"""Plan, apply and verify one unit's move to another production, journaled so a refusal resumes.

Usage: transfer_unit.py plan|apply|verify <from slug> <unit> <to production doc> [--scratch <dir>]

The giving showrunner runs it. The unit keeps its running session, worktree, branch and
unmerged commits. apply moves its staff membership and live tab through staff_move, moves the
Units row and associated state, then tells the unit and both showrunners. It never stops or
relaunches a session.

The doc commits are not pushed: each goes out with its showrunner's next merge push, as add_unit's
row commit does. The unit takes the receiving merge branch at its own next merge.

Version one moves a unit between productions of one repository on this machine: a unit on the Mac,
or a receiving production in another repository, is refused, since its worktree could not be kept.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from production_units import Production, read_production  # noqa: E402
import merge_checkpoint  # noqa: E402
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
from unit_move import PathMove, git  # noqa: E402
from scripts.production import staff, staff_sources, staff_move

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
    staff_id: staff.StaffId
    parent_staff_id: staff.StaffId
    step: int = 0
    # Commit receipts and the unit's session address survive a partial source update.
    plan_commit: str = ""
    to_commit: str = ""
    from_commit: str = ""
    unit_address: str = ""

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


TEXT_FIELDS = tuple(field.name for field in fields(UnitTransfer) if field.name not in ("step", "staff_id", "parent_staff_id"))
# The fields a fresh transfer fills; the rest start empty.
REQUIRED = TEXT_FIELDS[:TEXT_FIELDS.index("plan_commit")]
# These are current locations, not recovery state. Old journals may contain them.
LOCATION_FIELDS = ("from_doc", "to_doc", "from_checkout", "to_checkout")


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


def _production(slug: str) -> Production:
    checked = showrunners.checked_doc(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}")
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise RenameRefused(f"{slug} has no readable update timer: {checked.reason}")
    return read_production(checked.path)


def build(from_slug: str, given: str, to_doc: Path, scratch: Path) -> UnitTransfer:
    giving = _production(from_slug)
    receiving = read_production(to_doc.expanduser().resolve())
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
    record = staff.snapshot()
    member = staff_sources.member(staff_sources.UnitMember(giving.slug, unit), record)
    if isinstance(member, staff_sources.NoSuchMember):
        # A staff move can finish before this command moves the Units row.
        member = staff_sources.member(staff_sources.UnitMember(receiving.slug, unit), record)
    parent = staff_sources.member(staff_sources.ShowrunnerMember(receiving.slug), record)
    if isinstance(member, staff_sources.NoSuchMember) or isinstance(parent, staff_sources.NoSuchMember):
        raise staff.StaffRefused(f"{giving.slug}/{unit} to {receiving.slug}: no matching staff row")
    return UnitTransfer(giving.slug, receiving.slug, unit, str(giving.doc), str(receiving.doc),
                        str(giving.checkout), str(receiving.checkout), giving.merge_branch,
                        receiving.merge_branch, branch, str(worktree), row.get("Plan", ""),
                        git(worktree, "rev-parse", "HEAD"), str(scratch), member.row.id, parent.row.id,
                        unit_address=_unit_address(giving.slug, receiving.slug, unit, member.row.name))


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


def _check_doc(checkout: Path, merge_branch: str, doc: Path, expected: str = "") -> None:
    if git(checkout, "branch", "--show-current") != merge_branch:
        raise RenameRefused(f"{checkout} is not on {merge_branch}")
    if git(checkout, "status", "--porcelain", "--", str(doc)):
        # A journaled row step can have written (or staged) exactly its own edit before stopping.
        if not expected or doc.read_text(encoding="utf-8") != expected:
            raise RenameRefused(f"{doc} has uncommitted changes")
        relative = doc.relative_to(checkout)
        committed = _blob_text(checkout, f"HEAD:{relative}")
        staged = _blob_text(checkout, f":{relative}")
        if staged not in (committed, expected):
            raise RenameRefused(f"{doc} has uncommitted changes")


def _check_ready(transfer: UnitTransfer) -> None:
    """Check document and branch writes before moving staff membership or the live tab."""
    _check_doc(Path(transfer.to_checkout), transfer.to_merge_branch, Path(transfer.to_doc))
    _check_doc(Path(transfer.from_checkout), transfer.from_merge_branch, Path(transfer.from_doc))
    worktree = transfer.worktree()
    remote = unit_move.remote_tip(worktree, transfer.branch)
    if remote and subprocess.run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", remote, transfer.tip],
                                 capture_output=True, check=False).returncode:
        raise RenameRefused(f"origin {transfer.branch} has commits the local branch lacks; the push would fail")


def _commit_only(directory: Path, paths: list[Path], message: str) -> str:
    """Commit only `paths`, or adopt the commit an earlier run made before its journal recorded it."""
    if git(directory, "show", "-s", "--format=%s", "HEAD") == message:
        return git(directory, "rev-parse", "HEAD")
    relative = [str(path.relative_to(directory)) for path in paths]
    _ = git(directory, "commit", "--only", "-m", message, "--", *relative)
    return git(directory, "rev-parse", "HEAD")


# The steps. Each takes the transfer and a save callback, and is safe to run again.


def _header(transfer: UnitTransfer) -> str:
    doc = Path(transfer.to_doc).relative_to(Path(transfer.to_checkout))
    return f"> **Production: {transfer.to_slug}** — unit `{transfer.unit}`; production doc `{doc}`\n"


def _plans(transfer: UnitTransfer) -> list[Path]:
    return list(unit_move.plan_paths(transfer.plan_cell, transfer.worktree()))


def _plan_text(transfer: UnitTransfer, text: str) -> str:
    """The plan's `> **Production:**` header names the receiving production; its prose is history."""
    return "".join(_header(transfer) if line.startswith("> **Production") else line
                   for line in text.splitlines(keepends=True))


def _git_input(directory: Path, text: str, *arguments: str, index: Path | None = None) -> str:
    """Run git with `text` on stdin, and with its own index file when one is given."""
    environment = {**os.environ, **({"GIT_INDEX_FILE": str(index)} if index else {})}
    result = subprocess.run(["git", "-C", str(directory), *arguments], input=text, capture_output=True, text=True,
                            env=environment, check=False)
    if result.returncode:
        raise RenameRefused(f"git {' '.join(arguments)} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def _blob_text(directory: Path, blob: str) -> str:
    """A blob's exact text, its last newline included."""
    result = subprocess.run(["git", "-C", str(directory), "cat-file", "blob", blob], capture_output=True, check=False)
    if result.returncode:
        raise RenameRefused(f"git cat-file {blob} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout.decode("utf-8")


def _blob(directory: Path, text: str) -> str:
    return _git_input(directory, text, "hash-object", "-w", "--stdin")


def _header_commit(transfer: UnitTransfer) -> str:
    """Commit the header change to HEAD's plan files through a temporary index, so that the
    worktree's edits and the real index are not part of it. Returns HEAD when no header changes."""
    worktree = transfer.worktree()
    head = git(worktree, "rev-parse", "HEAD")
    changed = False
    with tempfile.TemporaryDirectory() as folder:
        index = Path(folder) / "index"
        _ = _git_input(worktree, "", "read-tree", head, index=index)
        for path in _plans(transfer):
            relative = str(path.relative_to(worktree))
            listed = git(worktree, "ls-tree", head, "--", relative).split()
            if not listed:
                continue
            text = _blob_text(worktree, listed[2])
            if (updated := _plan_text(transfer, text)) == text:
                continue
            _ = _git_input(worktree, "", "update-index", "--cacheinfo",
                           f"{listed[0]},{_blob(worktree, updated)},{relative}", index=index)
            changed = True
        if not changed:
            return head
        tree = _git_input(worktree, "", "write-tree", index=index)
    commit = _git_input(worktree, transfer.plan_message + "\n", "commit-tree", tree, "-p", head)
    _ = git(worktree, "update-ref", "-m", transfer.plan_message, f"refs/heads/{transfer.branch}", commit, head)
    return commit


def _carry_header(transfer: UnitTransfer) -> None:
    """Give the real index and the worktree file the same header change, so the unit's own edits
    stay and nothing shows as undoing the header commit."""
    worktree = transfer.worktree()
    for path in _plans(transfer):
        relative = str(path.relative_to(worktree))
        staged = git(worktree, "ls-files", "-s", "--", relative).split()
        if staged:
            text = _blob_text(worktree, staged[1])
            if (updated := _plan_text(transfer, text)) != text:
                _ = git(worktree, "update-index", "--cacheinfo", f"{staged[0]},{_blob(worktree, updated)},{relative}")
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if (updated := _plan_text(transfer, text)) != text:
                write_text(path, updated)


def _commit_plans(transfer: UnitTransfer, save: Callable[[], None]) -> None:
    """The unit reads its production from its plan's header, so the header moves on the unit branch.
    Work in progress stays as it is: the commit is made apart from the worktree (showrunner,
    2026-10-21: hierarchies-unit had a phase open with its plan edited)."""
    worktree = transfer.worktree()
    if not transfer.plan_commit:
        # A commit made before the journal recorded it is adopted by its subject.
        transfer.plan_commit = (git(worktree, "rev-parse", "HEAD")
                                if git(worktree, "show", "-s", "--format=%s", "HEAD") == transfer.plan_message
                                else _header_commit(transfer))
        save()
    _carry_header(transfer)
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
        row = _row(Path(transfer.from_doc), transfer.unit)
        if row is None:
            raise RenameRefused(f"{transfer.unit} has no Units row in {transfer.from_doc} to move")
        lines = _blob_text(checkout, f"HEAD:{doc.relative_to(checkout)}").splitlines()
        insert_at, _ = production_units.unit_rows(lines)
        lines.insert(insert_at, _row_line(row, production_units.unit_headings(lines)))
        expected = "\n".join(lines) + "\n"
        _check_doc(checkout, transfer.to_merge_branch, doc, expected)
        write_text(doc, expected)
    transfer.to_commit = _commit_only(checkout, [doc], transfer.to_message)
    save()


def _commit_from(transfer: UnitTransfer, save: Callable[[], None]) -> None:
    checkout, doc = Path(transfer.from_checkout), Path(transfer.from_doc)
    if transfer.from_commit:
        return
    if git(checkout, "show", "-s", "--format=%s", "HEAD") != transfer.from_message:
        lines = _blob_text(checkout, f"HEAD:{doc.relative_to(checkout)}").splitlines(keepends=True)
        start = lines.index("## Units\n")
        end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
        kept = [line for index, line in enumerate(lines) if not (
            start < index < end and line.startswith("|")
            and production_units.cell_value(production_units.row_cells(line)[0]) == transfer.unit)]
        expected = "".join(kept)
        _check_doc(checkout, transfer.from_merge_branch, doc, expected)
        write_text(doc, expected)
    transfer.from_commit = _commit_only(checkout, [doc], transfer.from_message)
    save()


def _move_state(transfer: UnitTransfer, _save: Callable[[], None]) -> None:
    """The stall-watch stretch moves to the receiving slug; the giving pool's reviewers retire, so
    the receiving pool leases its own. The running pane keeps its launch record and identity.
    The unit's held design roles name the receiving production as holder, so they resume under it.
    The phase note follows on the unit's next refresh, and the giving showrunner's report state
    keeps its rows as history."""
    from scripts.astra import design_roles
    from scripts.direct import workers
    workers.move_unit(transfer.from_slug, transfer.to_slug, transfer.unit,
                      runs=tuple(retire_unit.runs_of(Path(transfer.worktree_dir))))
    try:
        design_roles.move_holder(design_roles.UnitHolder(transfer.from_slug, transfer.unit),
                          design_roles.UnitHolder(transfer.to_slug, transfer.unit), reason="unit transfer")
    except (OSError, ValueError, SystemExit) as error:
        raise RenameRefused(f"held roles: {error}") from error
    for path in (stall_watch.stretch_path, stall_watch.prompt_path):
        unit_move.move(PathMove(path(transfer.from_slug, transfer.unit), path(transfer.to_slug, transfer.unit)))
    retired = subprocess.run([sys.executable, str(POOL), "retire", "--production", transfer.from_slug,
                              "--unit", transfer.unit], capture_output=True, text=True, check=False)
    if retired.returncode:
        raise RenameRefused(f"review pool: {(retired.stderr or retired.stdout).strip()}")


def _unit_address(from_slug: str, to_slug: str, unit: str, name: str) -> str:
    """Pin the session before its production's source row changes."""
    for slug in (from_slug, to_slug):
        found = unit_lookup.marked_units(slug).get(unit)
        if found is not None and isinstance(found.claude, unit_lookup.LiveClaude):
            return f"session:{found.claude.session_id}"
    return name


def moved_note(transfer: UnitTransfer) -> str:
    return (f"Message from {transfer.from_slug} showrunner: {transfer.unit} now belongs to {transfer.to_slug}.\n"
            + f"Your production doc is now {transfer.to_doc}, your merge branch is {transfer.to_merge_branch}, "
            + f"and your showrunner is the {transfer.to_slug} showrunner. Your worktree "
            + f"{transfer.worktree_dir}, your branch {transfer.branch} and its commits stay. Take "
            + f"origin/{transfer.to_merge_branch} at your next merge, and send checkpoints to the new showrunner.")


def _notice_key(transfer: UnitTransfer, recipient: str) -> str:
    return f"unit-transfer:{transfer.staff_id}:{transfer.from_slug}:{transfer.to_slug}:{transfer.tip}:{recipient}"


def _notice_result(delivery: showrunners.NoticeDelivery) -> None:
    if delivery.failed:
        raise RenameRefused("; ".join(delivery.lines))
    print("\n".join(delivery.lines))


def _tell_unit(transfer: UnitTransfer, _save: Callable[[], None]) -> None:
    address = transfer.unit_address or _unit_address(transfer.from_slug, transfer.to_slug,
                                                    transfer.unit, unit_names.name(transfer.unit))
    _notice_result(showrunners.send_to_address(address, _notice_key(transfer, "unit"), moved_note(transfer),
                                              recipient=transfer.unit, sender="transfer_unit"))


def _tell_from(transfer: UnitTransfer, _save: Callable[[], None]) -> None:
    _notice_result(showrunners.deliver(transfer.from_slug, _notice_key(transfer, "from"), _showrunner_note(transfer)))


def _tell_to(transfer: UnitTransfer, _save: Callable[[], None]) -> None:
    _notice_result(showrunners.deliver(transfer.to_slug, _notice_key(transfer, "to"), _showrunner_note(transfer)))


def _showrunner_note(transfer: UnitTransfer) -> str:
    return (f"Message from transfer_unit: {transfer.unit} moved from {transfer.from_slug} to {transfer.to_slug}.\n"
            + f"Its Units row is now in {transfer.to_doc}; its session keeps running.")


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
    record = staff.snapshot()
    row = record.by_id(transfer.staff_id)
    if row is None or row.parent != transfer.parent_staff_id:
        failures.append(f"staff: {unit} is not under {transfer.to_slug}")
    if leased := _leased(transfer.from_slug, unit):
        failures.append(f"review pool {transfer.from_slug}: {', '.join(leased)} still leased to {unit}")
    from scripts.astra import design_roles
    failures.extend(design_roles.holder_failures(design_roles.UnitHolder(transfer.from_slug, unit)))
    return failures


def _verify(transfer: UnitTransfer, _save: Callable[[], None]) -> None:
    if failures := verify_failures(transfer):
        raise RenameRefused("verify failed:\n  " + "\n  ".join(failures))


class Step(NamedTuple):
    run: Callable[[UnitTransfer, Callable[[], None]], None]
    describe: Callable[[UnitTransfer], str]
    # Messaging and verification do not hold the merge locks.
    locked: bool = True


def _describe_plans(t: UnitTransfer) -> str:
    return f"{t.branch}: commit {t.plan_message!r} (the plan's Production header names {t.to_slug}) and push it"


def _describe_to(t: UnitTransfer) -> str:
    return f"{t.to_merge_branch}: commit {t.to_message!r} (add the Units row)"


def _describe_from(t: UnitTransfer) -> str:
    return f"{t.from_merge_branch}: commit {t.from_message!r} (drop the Units row)"


def _describe_state(t: UnitTransfer) -> str:
    return (f"state: stall-watch stretch {t.from_slug} -> {t.to_slug}; held design roles' holder "
            + f"{t.from_slug} -> {t.to_slug}; retire the {t.from_slug} reviewers")


def _describe_tell_unit(t: UnitTransfer) -> str:
    return f"tell {t.unit} its new production, doc and merge branch"


def _describe_tell_from(t: UnitTransfer) -> str:
    return f"tell {t.from_slug} showrunner that {t.unit} moved to {t.to_slug}"


def _describe_tell_to(t: UnitTransfer) -> str:
    return f"tell {t.to_slug} showrunner that {t.unit} moved from {t.from_slug}"


def _describe_verify(t: UnitTransfer) -> str:
    return f"verify: {t.unit} shows under {t.to_slug} and nowhere under {t.from_slug}"


STEPS = (
    Step(_commit_plans, _describe_plans),
    Step(_commit_to, _describe_to),
    Step(_commit_from, _describe_from),
    Step(_move_state, _describe_state),
    Step(_tell_unit, _describe_tell_unit, locked=False),
    Step(_tell_from, _describe_tell_from, locked=False),
    Step(_tell_to, _describe_tell_to, locked=False),
    Step(_verify, _describe_verify, locked=False),
)


def journal(slug: str) -> Path:
    return tidy_wrapped.owned_paths(slug)[-1] / "transfer_unit.json"


def _load(path: Path, to_doc: Path) -> UnitTransfer:
    values = unit_move.json_object(read_json(path))
    values = _migrate_journal(path, values)
    return _load_fields(path, values, to_doc)


def _migrate_journal(path: Path, values: dict[str, object]) -> dict[str, object]:
    if not all(isinstance(values.get(name), str) and values[name] for name in ("from_slug", "to_slug", "unit")):
        raise RenameRefused(f"{path}: invalid unit transfer identity")
    return rename_state.migrate_staff_ids(path, values, {
        "staff_id": tuple(staff_sources.UnitMember(cast(str, values[name]), cast(str, values["unit"]))
                          for name in ("from_slug", "to_slug")),
        "parent_staff_id": (staff_sources.ShowrunnerMember(cast(str, values["to_slug"])),),
    })


def _load_fields(path: Path, values: dict[str, object], to_doc: Path) -> UnitTransfer:
    step = values.get("step")
    # The pre-live-move journal had stop/record/relaunch steps instead of notices.
    if values.get("version", 1) == 1 and type(step) is int:
        step = {0: 0, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 4, 7: 4, 8: len(STEPS)}.get(step, step)
    if (type(step) is not int
            or not all(isinstance(values.get(name, ""), str) for name in TEXT_FIELDS if name not in LOCATION_FIELDS)
            or not all(values.get(name) for name in REQUIRED if name not in LOCATION_FIELDS)
            or not all(isinstance(values.get(name), str) and values[name] for name in ("staff_id", "parent_staff_id"))):
        raise RenameRefused(f"{path}: invalid unit transfer journal")
    receiving = read_production(to_doc.expanduser().resolve())
    if receiving.slug != values["to_slug"]:
        raise RenameRefused(f"the recorded transfer is {values['unit']} to {values['to_slug']}")
    giving = _production(cast(str, values["from_slug"]))
    values.update(from_doc=str(giving.doc), from_checkout=str(giving.checkout),
                  to_doc=str(receiving.doc), to_checkout=str(receiving.checkout))
    return UnitTransfer(*(cast(str, values.get(name, "")) for name in TEXT_FIELDS[:len(REQUIRED)]), step=step,
                        staff_id=staff.StaffId(cast(str, values["staff_id"])),
                        parent_staff_id=staff.StaffId(cast(str, values["parent_staff_id"])),
                        **{name: cast(str, values.get(name, "")) for name in TEXT_FIELDS[len(REQUIRED):]})


def _save(path: Path, transfer: UnitTransfer) -> None:
    saved = cast(dict[str, object], asdict(transfer))
    for name in LOCATION_FIELDS:
        del saved[name]
    previous: list[dict[str, object]] = []
    if path.exists():
        current = unit_move.json_object(read_json(path))
        previous = _completed_requests(current)
        if current.get("done") is True and (current.get("unit"), current.get("to_slug")) != (transfer.unit, transfer.to_slug):
            previous.append({key: value for key, value in current.items() if key != "completed_requests"})
    if previous:
        saved["completed_requests"] = previous
    write_json(path, {**saved, "version": 2, "done": transfer.step == len(STEPS)})


def _completed_requests(values: dict[str, object]) -> list[dict[str, object]]:
    previous = values.get("completed_requests", [])
    if not isinstance(previous, list):
        raise RenameRefused("invalid completed transfer requests")
    return [unit_move.json_object(item) for item in cast(list[object], previous)]


def _resumed(path: Path, unit: str, to_doc: Path) -> UnitTransfer | None:
    """The unfinished transfer the journal holds, which must be this request; None without one."""
    if not path.exists():
        return None
    values = unit_move.json_object(read_json(path))
    receiving = read_production(to_doc.expanduser().resolve())
    candidates = [values, *_completed_requests(values)] if values.get("done") is True else [values]
    for fields in candidates:
        if fields.get("unit") in (unit, unit_names.unit_id(unit)) and fields.get("to_slug") == receiving.slug:
            if fields is values:
                fields = _migrate_journal(path, fields)
            return _load_fields(path, fields, to_doc)
    if values.get("done") is True:
        return None
    destination = _production(cast(str, values["to_slug"]))
    command = shlex.join(["transfer_unit.py", "apply", cast(str, values["from_slug"]),
                          cast(str, values["unit"]), str(destination.doc)])
    raise RenameRefused(f"the unfinished transfer is {values.get('unit')} to {values.get('to_slug')}; "
                        + f"resume it first with {command}")


@contextmanager
def _merge_locks(transfer: UnitTransfer) -> Generator[None]:
    """Both productions' merge locks, taken in slug order so two transfers cannot deadlock."""
    pairs = sorted(((transfer.from_slug, transfer.from_checkout), (transfer.to_slug, transfer.to_checkout)))
    with merge_checkpoint.merge_lock(Path(pairs[0][1]), pairs[0][0]):
        with merge_checkpoint.merge_lock(Path(pairs[1][1]), pairs[1][0]):
            yield


def describe(transfer: UnitTransfer) -> list[str]:
    return [f"1. move {transfer.unit}'s staff membership and live tab to {transfer.to_slug}",
            *(f"{number + 1}. {step.describe(transfer)}{'  (done)' if number <= transfer.step else ''}"
              for number, step in enumerate(STEPS, 1))]


def plan(from_slug: str, unit: str, to_doc: Path, scratch: Path) -> UnitTransfer:
    recorded = _resumed(journal(from_slug), unit, to_doc)
    if recorded is not None:
        return recorded
    transfer = build(from_slug, unit, to_doc, scratch)
    _check_journals(transfer)
    preflight(transfer)
    _check_ready(transfer)
    return transfer


def apply(from_slug: str, unit: str, to_doc: Path, scratch: Path) -> UnitTransfer:
    with staff.operation():
        return _apply(from_slug, unit, to_doc, scratch)


def _move_staff(transfer: UnitTransfer) -> None:
    _ = staff_move.move_unit(transfer.from_slug, transfer.unit, transfer.to_slug,
                             identity=staff_move.PinnedUnit(transfer.staff_id, transfer.parent_staff_id))


def _apply(from_slug: str, unit: str, to_doc: Path, scratch: Path) -> UnitTransfer:
    path = journal(from_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    with unit_move.production_lock():
        transfer = _resumed(path, unit, to_doc)
        if transfer is not None and transfer.step == len(STEPS):
            print("done; unchanged")
            return transfer
        fresh = transfer is None
        previous_journal = path.read_text(encoding="utf-8") if fresh and path.exists() else ""
        if transfer is None:
            transfer = build(from_slug, unit, to_doc, scratch)
            _check_journals(transfer)
            with _merge_locks(transfer):
                preflight(transfer)
                _check_ready(transfer)
            # The request is saved before anything changes, so every later refusal resumes.
            _save(path, transfer)
        try:
            _move_staff(transfer)
        except staff.StaffRefused:
            if fresh:
                if previous_journal:
                    write_text(path, previous_journal)
                else:
                    path.unlink(missing_ok=True)
            raise
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
    recorded = _load(path, to_doc)
    if recorded.unit not in (unit, unit_names.unit_id(unit)) or Path(recorded.to_doc) != to_doc.expanduser().resolve():
        raise RenameRefused(f"the recorded transfer is {recorded.unit} to {recorded.to_slug}")
    if recorded.step < len(STEPS) - 1:
        raise RenameRefused(f"{recorded.unit} to {recorded.to_slug} is at step {recorded.step}; finish it with apply")
    _verify(recorded, lambda: None)
    recorded.step = len(STEPS)
    _save(path, recorded)
    return recorded


def main(argv: list[str]) -> int:
    return staff.cli_result(lambda: _main(argv))


def _main(argv: list[str]) -> int:
    from scripts.direct.workers import WorkerUnavailable
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
    except (staff.StaffRefused, staff.StaffNotFound, staff.StaffUnreadable):
        raise
    except (RenameRefused, production_units.Refusal, unit_names.BadName, WorkerUnavailable, OSError, UnicodeError) as error:
        print(f"transfer_unit: refused: {error}", file=sys.stderr)
        return 1
    print("\n".join(describe(transfer)))
    if verb != "plan":
        print(f"{transfer.unit} is now in {transfer.to_slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
