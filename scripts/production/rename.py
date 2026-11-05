#!/usr/bin/env python3
"""Rename a showrunner row and repair its session, tab, and workspace names."""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.herdr import herdr
from scripts.production import staff, unit_move
from scripts.production.rename_state import RenameRefused, production_journal, read_json, write_json
from scripts.shutdown import remote


@dataclass(frozen=True)
class KeepDocBranch:
    """The request changes no document path or branch."""


@dataclass(frozen=True)
class DocBranchMove:
    source: Path
    destination: Path
    checkout: Path
    old_branch: str
    branch: str
    digest: str
    tracked: bool


@dataclass(frozen=True)
class RenamePlan:
    member: staff.StaffRow
    name: str
    move: KeepDocBranch | DocBranchMove


@dataclass(frozen=True)
class Showrunner:
    row: staff.StaffRow


@dataclass(frozen=True)
class MissingStaffReference:
    pass


@dataclass(frozen=True)
class OtherStaffKind:
    row: staff.StaffRow


def _member(reference: str, record: staff.StaffSnapshot) -> Showrunner | MissingStaffReference | OtherStaffKind:
    row = record.by_member_reference(reference) or record.by_path(reference)
    if row is not None:
        return Showrunner(row) if row.kind == "showrunner" else OtherStaffKind(row)
    matches = [row for row in record.find(kind="showrunner") if row.name == reference]
    if len(matches) > 1:
        raise staff.StaffRefused(f"{reference}: several showrunner rows match; use the staff id")
    return Showrunner(matches[0]) if matches else MissingStaffReference()


def _saved_move(fields: dict[str, object]) -> DocBranchMove:
    keys = ("source", "destination", "checkout", "old_branch", "branch", "digest")
    if not all(isinstance(fields.get(key), str) and fields[key] for key in keys):
        raise RenameRefused("the document move journal is incomplete")
    if not isinstance(fields.get("tracked"), bool):
        raise RenameRefused("the document move journal has no index evidence")
    return DocBranchMove(Path(cast(str, fields["source"])), Path(cast(str, fields["destination"])),
                         Path(cast(str, fields["checkout"])), cast(str, fields["old_branch"]),
                         cast(str, fields["branch"]), cast(str, fields["digest"]), cast(bool, fields["tracked"]))


def plan(old: str, new: str, *, to_doc: str = "", to_branch: str = "") -> RenamePlan:
    """Read the row and any explicit move without changing files."""
    if bool(to_doc) != bool(to_branch):
        raise RenameRefused("--to-doc and --to-branch are required together")
    record = staff.snapshot()
    target = _member(old, record)
    if isinstance(target, MissingStaffReference):
        target = _member(new, record)
    if isinstance(target, OtherStaffKind):
        raise staff.StaffRefused(f"{old}: the staff row is a {target.row.kind}, not a showrunner")
    if isinstance(target, MissingStaffReference):
        raise staff.StaffRefused(f"{old}: no showrunner staff row")
    row = target.row
    staff.validate_name(row.kind, new)
    if any(other.id != row.id and other.name == new
           for other in record.find(kind="showrunner")):
        raise staff.StaffRefused(f"{new}: a showrunner already has this name")
    if not to_doc:
        return RenamePlan(row, new, KeepDocBranch())
    doc = row.doc
    if isinstance(doc, staff.DocUnfilled):
        raise RenameRefused(f"{row.name}: the staff document is unfilled")
    if doc.machine != remote.local_machine():
        raise RenameRefused(f"{row.name}: run the document move on {doc.machine}")
    destination = Path(to_doc).expanduser().resolve()
    journal = production_journal(row.id)
    if journal.exists():
        value = read_json(journal)
        if not isinstance(value, dict):
            raise RenameRefused(f"{journal}: expected a journal object")
        fields = cast(dict[str, object], value)
        if (fields.get("staff_id") == row.id and fields.get("name") == new
                and fields.get("destination") == str(destination) and fields.get("branch") == to_branch):
            move = _saved_move(fields)
            if (fields.get("done") is True and doc.path == destination
                    and unit_move.git(move.checkout, "branch", "--show-current") == to_branch):
                return RenamePlan(row, new, KeepDocBranch())
            return RenamePlan(row, new, move)
        if fields.get("done") is not True:
            raise RenameRefused(f"{journal}: another document move is incomplete")
    checkout = Path(unit_move.git(doc.path.parent, "rev-parse", "--show-toplevel"))
    _ = destination.relative_to(checkout)
    _ = unit_move.git(checkout, "check-ref-format", "--branch", to_branch)
    branch = unit_move.git(checkout, "branch", "--show-current")
    if not branch:
        raise RenameRefused(f"{checkout}: the checkout has no current branch")
    if doc.path != destination and unit_move.exists(destination):
        raise RenameRefused(f"destination already exists: {destination}")
    if branch != to_branch and unit_move.branch_tip(checkout, to_branch):
        raise RenameRefused(f"branch already exists: {to_branch}")
    if doc.path == destination and branch == to_branch:
        return RenamePlan(row, new, KeepDocBranch())
    return RenamePlan(row, new, DocBranchMove(doc.path, destination, checkout, branch, to_branch,
                                            hashlib.sha256(doc.path.read_bytes()).hexdigest(),
                                            bool(unit_move.git(checkout, "ls-files", "--",
                                                               str(doc.path.relative_to(checkout))))))


def _journal_fields(request: RenamePlan, move: DocBranchMove, *, done: bool) -> dict[str, object]:
    return {"staff_id": request.member.id, "name": request.name, "source": str(move.source),
            "destination": str(move.destination), "checkout": str(move.checkout),
            "old_branch": move.old_branch, "branch": move.branch, "digest": move.digest,
            "tracked": move.tracked, "done": done}


def _move_doc(request: RenamePlan, move: DocBranchMove) -> bool:
    changed = False
    source = str(move.source.relative_to(move.checkout))
    destination = str(move.destination.relative_to(move.checkout))
    document = move.source if move.source.exists() else move.destination
    if not document.is_file():
        raise RenameRefused(f"{document}: the document is unavailable")
    if hashlib.sha256(document.read_bytes()).hexdigest() != move.digest:
        print(f"anomaly: document_move: {document}: the document changed after the move request", file=sys.stderr)
    if move.source != move.destination and move.source.exists():
        if unit_move.exists(move.destination):
            raise RenameRefused(f"destination already exists: {move.destination}")
        move.destination.parent.mkdir(parents=True, exist_ok=True)
        if move.tracked:
            _ = unit_move.git(move.checkout, "mv", "--", source, destination)
        else:
            _ = move.source.rename(move.destination)
        changed = True
    if not move.destination.is_file():
        raise RenameRefused(f"{move.destination}: the moved document is unavailable")
    if move.tracked and move.source != move.destination:
        if unit_move.git(move.checkout, "ls-files", "--", source):
            _ = unit_move.git(move.checkout, "rm", "--cached", "--quiet", "--", source)
            changed = True
        if not unit_move.git(move.checkout, "ls-files", "--", destination):
            _ = unit_move.git(move.checkout, "add", "--", destination)
            changed = True
    row = staff.by_id(request.member.id)
    if row is None or isinstance(row.doc, staff.DocUnfilled):
        raise RenameRefused(f"{request.member.id}: the staff document is unavailable")
    if row.doc.path not in (move.source, move.destination):
        raise RenameRefused(f"{row.id}: the staff document changed during the move")
    if row.doc.path != move.destination:
        _ = staff.set_doc(row.id, replace(row.doc, path=move.destination))
        changed = True
    return changed


def _move_branch(move: DocBranchMove) -> bool:
    branch = unit_move.git(move.checkout, "branch", "--show-current")
    if branch == move.branch:
        return False
    if branch != move.old_branch:
        raise RenameRefused(f"{move.checkout}: the current branch changed during the move")
    if unit_move.branch_tip(move.checkout, move.branch):
        raise RenameRefused(f"branch already exists: {move.branch}")
    _ = unit_move.git(move.checkout, "branch", "-m", move.branch)
    return True


def _set_displays(row: staff.StaffRow) -> bool:
    """Use the shared repair for the row's current session and displays."""
    from scripts.production.rename_unit import repair_displays

    return repair_displays(row)


def _describe(request: RenamePlan) -> list[str]:
    lines = [f"Rename staff {request.member.id}: {request.member.name} -> {request.name}",
             f"Repair the current session, tab, and workspace names to {request.name}"]
    if isinstance(request.move, DocBranchMove):
        lines.extend([f"Journal the move at {production_journal(request.member.id)}",
                      f"Move document {request.move.source} -> {request.move.destination}; then set the staff document",
                      f"Rename branch {request.move.old_branch} -> {request.move.branch}"])
    return lines


def apply(old: str, new: str, *, to_doc: str = "", to_branch: str = "") -> RenamePlan:
    """Rename the row first; report other failures and continue with display repair."""
    with staff.operation():
        request = plan(old, new, to_doc=to_doc, to_branch=to_branch)
        move = request.move
        journal = production_journal(request.member.id)
        if isinstance(move, DocBranchMove) and not journal.exists():
            write_json(journal, _journal_fields(request, move, done=False))
        elif isinstance(move, DocBranchMove):
            saved = read_json(journal)
            if saved != _journal_fields(request, move, done=False) and saved != _journal_fields(request, move, done=True):
                write_json(journal, _journal_fields(request, move, done=False))
        row = request.member
        changed = row.name != request.name
        if changed:
            row = staff.rename(row.id, request.name)
        if isinstance(move, DocBranchMove):
            try:
                changed = _move_doc(request, move) or changed
                changed = _move_branch(move) or changed
                completed = _journal_fields(request, move, done=True)
                if read_json(journal) != completed:
                    write_json(journal, completed)
            except (OSError, ValueError, staff.StaffRefused, staff.StaffNotFound) as error:
                print(f"anomaly: document_move: {error}", file=sys.stderr)
                changed = True
        try:
            changed = _set_displays(row) or changed
        except (OSError, ValueError, staff.StaffRefused, staff.StaffNotFound, herdr.HerdrFailure) as error:
            print(f"anomaly: set_displays: {error}", file=sys.stderr)
            changed = True
        print("done" if changed else "unchanged")
        return request


def main(argv: list[str]) -> int:
    return staff.cli_result(lambda: _main(argv))


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "apply"):
        command = actions.add_parser(action)
        _ = command.add_argument("old")
        _ = command.add_argument("new")
        _ = command.add_argument("--to-doc", default="")
        _ = command.add_argument("--to-branch", default="")
    args = parser.parse_args(argv)
    try:
        old, new = cast(str, args.old), cast(str, args.new)
        options = {"to_doc": cast(str, args.to_doc), "to_branch": cast(str, args.to_branch)}
        if cast(str, args.action) == "plan":
            for line in _describe(plan(old, new, **options)):
                print(line)
        else:
            _ = apply(old, new, **options)
    except (ValueError, OSError, UnicodeError) as error:
        print(f"rename: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
