#!/usr/bin/env python3
"""Rename a staff unit, its folder, its Git branch, and its displays."""
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
from scripts.message import sessions


@dataclass(frozen=True)
class UnitRename:
    member: staff.StaffRow
    name: str
    destination: Path


def plan(slug: str, old: str, new: str) -> UnitRename:
    record = staff.snapshot()
    found = staff_sources.member(staff_sources.UnitMember(slug, old), record)
    if isinstance(found, staff_sources.NoSuchMember):
        found = staff_sources.member(staff_sources.UnitMember(slug, new), record)
    if isinstance(found, staff_sources.NoSuchMember):
        raise ValueError(f"{slug}/{old}: no unit staff row")
    if old != new:
        if isinstance(found.row.worktree, staff.WorktreeUnfilled):
            raise ValueError(f"{old}: the staff worktree is unfilled")
        if found.row.worktree.machine != remote.local_machine():
            raise ValueError(f"{old}: run rename on {found.row.worktree.machine}")
    name = unit_names.name(new)
    return UnitRename(found.row, name, Path.home() / "worktrees" / name)


def relocate_staff_worktree(member_id: staff.StaffId, worktree: Path) -> None:
    """Keep the location state when a production rename moves a unit."""
    row = staff.by_id(member_id)
    if row is None or isinstance(row.worktree, staff.WorktreeUnfilled):
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


def apply(slug: str, old: str, new: str) -> UnitRename:
    with staff.operation():
        request = plan(slug, old, new)
        row = request.member
        changed = row.name != request.name
        if changed:
            row = staff.rename(row.id, request.name)
        location = row.worktree
        if old != new and isinstance(location, staff.WorktreeUnfilled):
            raise ValueError(f"{row.name}: the staff worktree is unfilled")
        if old != new and not isinstance(location, staff.WorktreeUnfilled) and location.path != request.destination:
            if isinstance(location, staff.PresentWorktree):
                unit_move.move_folder(row.id, request.destination)
            else:
                request.destination.parent.mkdir(parents=True, exist_ok=True)
                if location.path.exists():
                    _ = unit_move.git(location.path, "worktree", "move", str(location.path), str(request.destination))
                relocate_staff_worktree(row.id, request.destination)
            changed = True
        if old == new:
            pass
        elif request.destination.is_dir():
            branch = unit_move.git(request.destination, "branch", "--show-current")
            if branch != request.name:
                _ = unit_move.git(request.destination, "branch", "-m", request.name)
                changed = True
        else:
            print(f"anomaly: folder is absent: {request.destination}", file=sys.stderr)
        try:
            changed = repair_displays(row) or changed
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            print(f"anomaly: display: {error}", file=sys.stderr)
            changed = True
        print("done" if changed else "unchanged")
        return request


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("plan", "apply", "verify"))
    _ = parser.add_argument("slug")
    _ = parser.add_argument("old")
    _ = parser.add_argument("new")
    _ = parser.add_argument("--scratch", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    def run() -> int:
        try:
            if cast(str, args.action) == "plan":
                request = plan(cast(str, args.slug), cast(str, args.old), cast(str, args.new))
                print(f"Rename {request.member.name} to {request.name}; folder {request.destination}")
            else:
                _ = apply(cast(str, args.slug), cast(str, args.old), cast(str, args.new))
            return 0
        except (OSError, ValueError, RenameRefused, herdr.HerdrFailure) as error:
            print(f"rename_unit: {error}", file=sys.stderr)
            return 1
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
