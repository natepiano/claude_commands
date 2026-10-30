"""Write the staff and source edges behind showrunners.py attach and detach."""
from __future__ import annotations

import sys
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import showrunners
import showrunner_tree
from scripts.production import staff, staff_sources
from scripts.lib.atomic_file import write_text_atomic


def attach(child: str, parent: str, *, record: staff.StaffSnapshot | None = None) -> None:
    """Store the checked desired parent; repeat calls keep the same file."""
    child, parent = showrunners.canonical_slug(child), showrunners.canonical_slug(parent)
    showrunner_tree.check_attach(child, parent, record=record)
    path = showrunners.SHOWRUNNER_STATE_DIR / child / showrunners.PARENT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, f"{parent}\n")


def detach(child: str, *, record: staff.StaffSnapshot | None = None) -> str:
    """Remove the parent file, returning its former parent or an empty string."""
    child = showrunners.canonical_slug(child)
    parent = showrunner_tree.parent_of(child, record=record)
    (showrunners.SHOWRUNNER_STATE_DIR / child / showrunners.PARENT_FILE).unlink(missing_ok=True)
    return parent


@dataclass(frozen=True)
class PendingParentNotice:
    action: Literal["attach", "detach"]
    parent: str


@dataclass(frozen=True)
class NoPendingParentNotice:
    """No routing notice remains to be sent for the previous parent change."""


def _notice_file(child: str) -> Path:
    return showrunners.SHOWRUNNER_STATE_DIR / child / "parent_notice.json"


def _pending_notice(child: str) -> PendingParentNotice | NoPendingParentNotice:
    path = _notice_file(child)
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoPendingParentNotice()
    except (OSError, ValueError) as error:
        raise staff.StaffUnreadable(f"{path}: {error}") from error
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        action, parent = fields.get("action"), fields.get("parent")
        if action in ("attach", "detach") and isinstance(parent, str):
            return PendingParentNotice(action, parent)
    raise staff.StaffUnreadable(f"{path}: invalid pending parent notice")


def _save_notice(child: str, notice: PendingParentNotice) -> None:
    path = _notice_file(child)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({"action": notice.action, "parent": notice.parent}, sort_keys=True) + "\n")


def _tell(child: str, texts: dict[str, str]) -> None:
    notices = showrunners.tell_others(texts, f"showrunner-tree:{child}")
    for line in notices.lines:
        print(line)
    if not notices.failed:
        _notice_file(child).unlink(missing_ok=True)


def _row(slug: str, record: staff.StaffSnapshot) -> staff.StaffRow:
    found = staff_sources.member(staff_sources.ShowrunnerMember(slug), record)
    if isinstance(found, staff_sources.NoSuchMember):
        raise staff.StaffRefused(f"{slug}: no matching showrunner staff row")
    return found.row


def attach_parent(child: str, parent: str = "") -> None:
    with staff.operation():
        child = showrunners.canonical_slug(child)
        parent = parent or showrunners.own_slug()
        if not parent:
            raise ValueError("name the parent: this session runs no production")
        parent = showrunners.canonical_slug(parent)
        record = staff.snapshot()
        try:
            showrunner_tree.check_attach(child, parent, record=record)
        except showrunner_tree.ParentCycle as error:
            raise staff.StaffRefused(str(error)) from error
        pending = _pending_notice(child)
        if isinstance(pending, PendingParentNotice) and pending != PendingParentNotice("attach", parent):
            raise staff.StaffRefused(f"{child}: finish its pending {pending.action} to {pending.parent} first")
        child_row, parent_row = _row(child, record), _row(parent, record)
        finished = child_row.parent == parent_row.id and isinstance(pending, NoPendingParentNotice)
        if finished:
            print(f"{child} already reports to {parent}; done")
            return
        if isinstance(pending, NoPendingParentNotice):
            _save_notice(child, PendingParentNotice("attach", parent))
        if child_row.parent != parent_row.id:
            _ = staff.move(child_row.id, parent_row.id)
        attach(child, parent, record=record)
        top = showrunner_tree.root_of(parent, record=record)
        print(f"{child} now reports to {parent}")
        _tell(child, {
            child: f"{child} now reports to {parent}. The root is {top}: send it each item for the user. {showrunners.RULES}",
            parent: f"{child} now reports to {parent}. It is a child showrunner, never a unit: it has no Units "
                    + f"row, and no broadcast is passed on to it. {showrunners.RULES}",
        })


def detach_parent(child: str) -> None:
    with staff.operation():
        child = showrunners.canonical_slug(child)
        pending = _pending_notice(child)
        if isinstance(pending, PendingParentNotice) and pending.action != "detach":
            raise staff.StaffRefused(f"{child}: finish its pending {pending.action} to {pending.parent} first")
        record = staff.snapshot()
        parent = showrunner_tree.parent_of(child, record=record)
        if not parent and isinstance(pending, PendingParentNotice):
            parent = pending.parent
        row = _row(child, record)
        if parent and isinstance(pending, NoPendingParentNotice):
            _save_notice(child, PendingParentNotice("detach", parent))
        if row.parent is not None:
            _ = staff.move(row.id, None)
        _ = detach(child, record=record)
        if not parent:
            print(f"{child} reports to no one; done")
            return
        print(f"{child} no longer reports to {parent}")
        text = f"{child} no longer reports to {parent}. {child} is a root again."
        _tell(child, {child: text, parent: text})


def main(action: str, child: str, parent: str = "") -> int:
    def run() -> int:
        try:
            if action == "attach":
                attach_parent(child, parent)
            elif action == "detach":
                detach_parent(child)
            else:
                raise ValueError(f"unknown parent operation: {action}")
        except ValueError as error:
            if isinstance(error, staff.StaffRefused):
                raise
            print(f"showrunners: {error}", file=sys.stderr)
            return 1
        return 0

    return staff.cli_result(run)
