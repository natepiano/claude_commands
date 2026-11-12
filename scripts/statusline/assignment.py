"""Read the session's assignment from its staff row."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

from scripts.production import staff
from scripts.statusline import adhoc_items, rebuild_row


def assignment(record: staff.StaffSnapshot, session_id: str) -> tuple[str, str]:
    """Return the assignment text and its color role."""
    row = record.by_current_session(session_id)
    if row is None:
        return "unassigned", "unassigned"
    if row.kind == "showrunner":
        return f"{row.name} showrunner", "showrunner"
    if row.kind == "worker":
        return "worker", "worker"
    parent = record.parent_of(row)
    label = f"{row.name} unit"
    if parent is not None:
        label += f" for {parent.name}"
    return label, "unit-director"


def staff_record(text: str) -> staff.StaffSnapshot:
    """Read staff rows without requiring each parent row in the display snapshot."""
    try:
        value = cast(dict[str, object], json.loads(text))
        if value["schema"] not in (1, 2):
            return staff.StaffSnapshot(())
        rows = cast(list[object], value["rows"])
        return staff.StaffSnapshot(tuple(staff.StaffRow.from_json(row) for row in rows))
    except (ValueError, KeyError, TypeError, staff.StaffUnreadable):
        return staff.StaffSnapshot(())


def current_branch(payload: dict[str, object], pwd: str) -> str:
    """Read the branch from the repository that contains the displayed folder."""
    workspace = cast(dict[str, object], payload.get("workspace") or {})
    folder = workspace.get("current_dir")
    if folder is None or folder is False:
        folder = payload.get("cwd")
    try:
        directory = Path(str(folder or pwd)).resolve()
        for parent in (directory, *directory.parents):
            git = parent / ".git"
            if git.is_dir():
                gitdir = git
            elif git.exists():
                entry = git.read_text(encoding="utf-8").strip()
                if not entry.startswith("gitdir:"):
                    return ""
                target = entry.removeprefix("gitdir:").strip()
                if not target:
                    return ""
                gitdir = parent / target
            else:
                continue
            head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
            if head.startswith("ref: refs/heads/"):
                return head.removeprefix("ref: refs/heads/")
            return f"detached {head[:7]}" if head else ""
    except (OSError, UnicodeError, ValueError):
        return ""
    return ""


def main() -> None:
    payload = cast(dict[str, object], json.load(sys.stdin))
    try:
        record = staff_record(Path(os.environ["STAFF_FILE"]).read_text(encoding="utf-8"))
    except OSError:
        record = staff.StaffSnapshot(())
    session_id = str(payload.get("session_id", ""))
    label, role = assignment(record, session_id)
    pwd = next((value for option, name, value in zip(sys.argv, sys.argv[1:], sys.argv[2:])
                if option == "--arg" and name == "pwd"), os.getcwd())
    branch = current_branch(payload, pwd) if role == "unit-director" else ""
    rebuild_config = Path(__file__).resolve().parents[2] / "config/statusline.conf"
    rebuild = rebuild_row.read(rebuild_config)
    if rebuild.refresh_due:
        try:
            _ = subprocess.Popen(
                [sys.executable, "-I", str(Path(rebuild_row.__file__).resolve()), "refresh", "--config", str(rebuild_config)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            pass
    os.execvp("jq", ["jq", "-n", "--argjson", "payload", json.dumps(payload),
                     "--arg", "assignment", label, "--arg", "role", role,
                     "--arg", "branch", branch,
                     "--arg", "adhoc", adhoc_items.row(session_id),
                     "--arg", "rebuild", rebuild.row(), *sys.argv[1:]])


if __name__ == "__main__":
    main()
