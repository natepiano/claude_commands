#!/usr/bin/env python3
"""Rename a staff unit, its displays, its folder and its branch."""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
for directory in ("production", "shutdown", "herdr", "direct", "lib", "style"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / directory))

from scripts.production import staff, staff_sources, unit_names, unit_move
from scripts.shutdown import remote
from scripts.herdr import herdr
from scripts.production.rename_state import RenameRefused
from scripts.lib.atomic_file import write_text_atomic
from scripts.agents import worker_lifecycle
from scripts.message import sessions


@dataclass(frozen=True)
class UnitRename:
    member: staff.StaffRow
    name: str


def plan(production_name: str, old: str, new: str) -> UnitRename:
    record = staff.snapshot()
    found = staff_sources.member(staff_sources.UnitMember(production_name, old), record)
    if isinstance(found, staff_sources.NoSuchMember):
        found = staff_sources.member(staff_sources.UnitMember(production_name, new), record)
    if isinstance(found, staff_sources.NoSuchMember):
        raise ValueError(f"{production_name}/{old}: no unit staff row")
    name = unit_names.name(new)
    if (found.row.name != name and not isinstance(found.row.worktree, staff.WorktreeUnfilled)
            and found.row.worktree.machine != remote.local_machine()):
        raise ValueError(f"{old}: run rename on {found.row.worktree.machine}")
    return UnitRename(found.row, name)


def relocate_staff_worktree(member_id: staff.StaffId, worktree: Path) -> None:
    """Keep the location state when a production rename moves a unit."""
    if (row := staff.by_id(member_id)) is None or isinstance(row.worktree, staff.WorktreeUnfilled):
        raise ValueError(f"{member_id}: no unit worktree")
    _ = staff.set_worktree(row.id, replace(row.worktree, path=worktree), expected=row.worktree)


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


def apply(production_name: str, old: str, new: str) -> UnitRename:
    request = plan(production_name, old, new)
    with staff.row_operation(str(request.member.id)):
        if (row := staff.by_id(request.member.id)) is None:
            raise ValueError(f"{production_name}/{old}: no unit staff row")
        changed = row.name != request.name
        pending = unit_move.pending_move(row.id)
        if isinstance(pending, unit_move.FolderMove) and changed:
            if not unit_move.move_folder(row.id, pending.destination, branch=pending.branch):
                print("unchanged")
                return request
            if (row := staff.by_id(request.member.id)) is None:
                raise ValueError(f"{production_name}/{old}: no unit staff row")
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
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("plan", "apply", "verify"))
    _ = parser.add_argument("production_name")
    _ = parser.add_argument("old")
    _ = parser.add_argument("new")
    _ = parser.add_argument("--scratch", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    def run() -> int:
        try:
            if cast(str, args.action) == "plan":
                request = plan(cast(str, args.production_name), cast(str, args.old), cast(str, args.new))
                print(f"Rename {request.member.name} to {request.name}")
            else:
                _ = apply(cast(str, args.production_name), cast(str, args.old), cast(str, args.new))
            return 0
        except worker_lifecycle.OperationDeadlineExpired as error:
            print(f"rename_unit: anomaly: {error}", file=sys.stderr)
            return 0
        except (OSError, ValueError, RenameRefused, herdr.HerdrFailure, subprocess.TimeoutExpired) as error:
            message = f"error: {str(error).splitlines()[0]}" if isinstance(error, subprocess.TimeoutExpired) else str(error)
            print(f"rename_unit: {message}", file=sys.stderr)
            return 1
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
