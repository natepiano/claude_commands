#!/usr/bin/env python3
"""Rename a unit or showrunner and repair its current session and displays."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast


from scripts.herdr import herdr
from scripts.production import staff, unit_move, unit_names
from scripts.production.rename_state import RenameRefused, production_journal, read_json, write_json
from scripts.shutdown import remote
from scripts.agents import worker_lifecycle
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import sessions


@dataclass(frozen=True)
class KeepDocBranch:
    """The request changes no document path or branch."""


@dataclass(frozen=True)
class StagedDocumentBlob:
    mode: str
    object_id: str


@dataclass(frozen=True)
class NoStagedDocumentBlob:
    """The journal has no staged document content to restore."""


@dataclass(frozen=True)
class DocBranchMove:
    source: Path
    destination: Path
    checkout: Path
    old_branch: str
    branch: str
    digest: str
    tracked: bool
    staged_blob: StagedDocumentBlob | NoStagedDocumentBlob


@dataclass(frozen=True)
class RenamePlan:
    member: staff.StaffRow
    name: str
    move: KeepDocBranch | DocBranchMove


def _member(old: str, new: str, record: staff.StaffSnapshot) -> staff.StaffRow:
    references = (old, f"{old.rpartition('/')[0]}/{new}" if "/" in old else new)
    for reference in references:
        row = record.by_member_reference(reference)
        if row is None and "/" in reference:
            row = record.by_path(reference) or record.by_path(reference.removesuffix("-unit"))
        if row is not None:
            return row
    rows = [row for row in record.rows if row.kind in {"showrunner", "unit"}]
    caller = record.by_current_session(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    local = ([row for row in rows if row.id == caller.id or row.parent == staff.ReportsTo(caller.id)]
             if caller is not None and caller.kind == "showrunner" else [])
    for scope in (local, rows):
        for reference in references:
            matches = [row for row in scope if row.name == reference or
                       row.kind == "unit" and row.name == reference.removesuffix("-unit")]
            if len(matches) > 1:
                raise staff.StaffRefused(f"{old}: several rows match; use showrunner/name or the staff id")
            if matches:
                return matches[0]
    raise staff.StaffRefused(f"{old}: no showrunner or unit staff row")


def _saved_move(fields: dict[str, object]) -> DocBranchMove:
    keys = ("source", "destination", "checkout", "old_branch", "branch", "digest")
    if not all(isinstance(fields.get(key), str) and fields[key] for key in keys):
        raise RenameRefused("the document move journal is incomplete")
    if not isinstance(fields.get("tracked"), bool):
        raise RenameRefused("the document move journal has no index evidence")
    blob = fields.get("staged_blob")
    staged_blob: StagedDocumentBlob | NoStagedDocumentBlob = NoStagedDocumentBlob()
    if isinstance(blob, dict):
        blob_fields = cast(dict[str, object], blob)
        mode = blob_fields.get("mode")
        object_id = blob_fields.get("object_id")
        if not isinstance(mode, str) or not mode or not isinstance(object_id, str) or not object_id:
            raise RenameRefused("the document move journal has an incomplete staged blob")
        staged_blob = StagedDocumentBlob(mode, object_id)
    elif "staged_blob" in fields:
        raise RenameRefused("the document move journal has an incomplete staged blob")
    return DocBranchMove(Path(cast(str, fields["source"])), Path(cast(str, fields["destination"])),
                         Path(cast(str, fields["checkout"])), cast(str, fields["old_branch"]),
                         cast(str, fields["branch"]), cast(str, fields["digest"]), cast(bool, fields["tracked"]),
                         staged_blob)


def plan(old: str, new: str, *, to_doc: str = "", to_branch: str = "") -> RenamePlan:
    """Read the row and any explicit move without changing files."""
    if bool(to_doc) != bool(to_branch):
        raise RenameRefused("--to-doc and --to-branch are required together")
    record = staff.snapshot()
    row = _member(old, new, record)
    if row.kind != "showrunner" and (row.kind != "unit" or to_doc):
        raise staff.StaffRefused(f"{old}: the staff row is a {row.kind}, not a showrunner")
    if row.kind == "unit":
        name = unit_names.name(new)
        if (row.name != name and not isinstance(row.worktree, staff.WorktreeUnfilled)
                and row.worktree.machine != remote.local_machine()):
            raise ValueError(f"{old}: run rename on {row.worktree.machine}")
        return RenamePlan(row, name, KeepDocBranch())
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
    entry = unit_move.git(checkout, "ls-files", "-s", "--", str(doc.path.relative_to(checkout)))
    staged_blob: StagedDocumentBlob | NoStagedDocumentBlob = NoStagedDocumentBlob()
    parts = entry.split(maxsplit=3)
    if len(entry.splitlines()) == 1 and len(parts) == 4 and parts[2] == "0":
        staged_blob = StagedDocumentBlob(parts[0], parts[1])
    return RenamePlan(row, new, DocBranchMove(doc.path, destination, checkout, branch, to_branch,
                                            hashlib.sha256(doc.path.read_bytes()).hexdigest(),
                                            bool(entry), staged_blob))


def _journal_fields(request: RenamePlan, move: DocBranchMove, *, done: bool) -> dict[str, object]:
    fields: dict[str, object] = {
        "staff_id": request.member.id, "name": request.name, "source": str(move.source),
        "destination": str(move.destination), "checkout": str(move.checkout),
        "old_branch": move.old_branch, "branch": move.branch, "digest": move.digest,
        "tracked": move.tracked, "done": done,
    }
    if isinstance(move.staged_blob, StagedDocumentBlob):
        fields["staged_blob"] = {"mode": move.staged_blob.mode, "object_id": move.staged_blob.object_id}
    return fields


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
        if not unit_move.git(move.checkout, "ls-files", "--", destination):
            if isinstance(move.staged_blob, StagedDocumentBlob):
                _ = unit_move.git(move.checkout, "update-index", "--add", "--cacheinfo",
                                  f"{move.staged_blob.mode},{move.staged_blob.object_id},{destination}")
            else:
                _ = unit_move.git(move.checkout, "add", "--", destination)
            changed = True
        if unit_move.git(move.checkout, "ls-files", "--", source):
            _ = unit_move.git(move.checkout, "rm", "--cached", "--quiet", "--", source)
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


def repair_displays(row: staff.StaffRow) -> bool:
    """Repair the current session name and its pane, tab, and workspace labels."""
    changed = False
    for session in sessions.running_sessions():
        if session["sessionId"] != row.current_session or session["name"] == row.name:
            continue
        path = sessions.sessions_dir() / f"{session['pid']}.json"
        raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        current = sessions.parse_session(raw)
        if (isinstance(current, sessions.UnreadableSessionRecord)
                or current["sessionId"] != row.current_session or current["pid"] != session["pid"]
                or current.get("procStart") != session.get("procStart") or not sessions.running_session(current)):
            raise ValueError(f"{row.name}: the session changed during rename")
        if current["name"] == row.name:
            continue
        raw["name"] = row.name
        raw["formerNames"] = list(dict.fromkeys([*current["formerNames"], current["name"]]))
        write_text_atomic(path, json.dumps(raw, indent=2) + "\n")
        changed = True
    client = herdr.Herdr(herdr.shared_session())
    spaces: set[str] = set()
    for pane in client.panes():
        native_sessions: set[str] = set()
        for kind in ("claude", "codex"):
            try:
                native_sessions.add(client.native_session(pane["pane_id"], kind, herdr.PROGRAM_TIMEOUT_SECONDS))
            except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError):
                continue
        if native_sessions != {row.current_session}:
            if pane.get("session_id") == row.current_session:
                print(f"anomaly: pane {pane['pane_id']} does not run session {row.current_session}", file=sys.stderr)
                changed = True
            continue
        changed = client.retitle(pane["pane_id"], row.name) or changed
        spaces.add(pane["workspace_id"])
    if row.kind == "showrunner" and spaces:
        for space in client.listed("workspace"):
            if space.get("workspace_id") in spaces and space.get("label") != row.name:
                _ = client.run("workspace", "rename", str(space["workspace_id"]), row.name)
                changed = True
        if changed:
            client.save_layout()
    return changed


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
    request = plan(old, new, to_doc=to_doc, to_branch=to_branch)
    if request.member.kind == "unit":
        return _apply_unit(request)
    with staff.operation():
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
            changed = repair_displays(row) or changed
        except (OSError, ValueError, staff.StaffRefused, staff.StaffNotFound, herdr.HerdrFailure) as error:
            print(f"anomaly: set_displays: {error}", file=sys.stderr)
            changed = True
        print("done" if changed else "unchanged")
        return request


def _apply_unit(request: RenamePlan) -> RenamePlan:
    with staff.row_operation(str(request.member.id)):
        if (row := staff.by_id(request.member.id)) is None:
            raise ValueError(f"{request.member.id}: no unit staff row")
        changed = row.name != request.name
        pending = unit_move.pending_move(row.id)
        if isinstance(pending, unit_move.FolderMove) and changed:
            if not unit_move.move_folder(row.id, pending.destination, branch=pending.branch):
                print("unchanged")
                return request
            if (row := staff.by_id(request.member.id)) is None:
                raise ValueError(f"{request.member.id}: no unit staff row")
            pending = unit_move.pending_move(row.id)
        movement = pending
        if isinstance(pending, unit_move.PendingRename) and pending.name != request.name:
            raise ValueError(f"{row.name}: finish the pending rename to {pending.name}")
        if isinstance(pending, unit_move.NoPendingMove) and changed:
            if isinstance(row.worktree, staff.WorktreeUnfilled):
                print(f"anomaly: {request.name}: the staff worktree is unfilled", file=sys.stderr)
            else:
                if row.worktree.machine != remote.local_machine():
                    raise ValueError(f"{row.name}: run rename on {row.worktree.machine}")
                folder = row.worktree.path.expanduser().resolve()
                target = unit_names.swap_stem(folder.name, row.name, request.name)
                destination = folder.with_name(target) if target is not None else folder
                checkout = folder if folder.exists() else destination
                branch = unit_move.git(checkout, "branch", "--show-current") if checkout.exists() else ""
                renamed_branch = unit_names.swap_stem(branch, row.name, request.name)
                if target is None:
                    print(f"anomaly: folder {folder} does not end with -{row.name}", file=sys.stderr)
                if renamed_branch is None:
                    print(f"anomaly: branch {branch} does not end with -{row.name}", file=sys.stderr)
                target_branch = renamed_branch if renamed_branch is not None else unit_move.KeepBranch.CURRENT
                if target is not None or renamed_branch is not None:
                    movement = unit_move.PendingRename(destination, target_branch, request.name)
                    unit_move.prepare_move(row, destination, target_branch, request.name)
        if changed:
            row = staff.rename(row.id, request.name)
        try:
            changed = repair_displays(row) or changed
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            print(f"anomaly: display: {error}", file=sys.stderr)
            changed = True
        if isinstance(movement, (unit_move.FolderMove, unit_move.PendingRename)):
            changed = unit_move.move_folder(row.id, movement.destination, branch=movement.branch) or changed
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
    except worker_lifecycle.OperationDeadlineExpired as error:
        print(f"rename: anomaly: {error}", file=sys.stderr)
        return 0
    except subprocess.TimeoutExpired as error:
        print(f"rename: error: {str(error).splitlines()[0]}", file=sys.stderr)
        return 1
    except (ValueError, OSError, UnicodeError, RenameRefused, herdr.HerdrFailure) as error:
        print(f"rename: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
