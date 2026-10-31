"""Resolve a conversation's session labels from its current staff owner."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from scripts.production import staff, showrunners, unit_names
from scripts.shutdown import remote


def showrunner_owner(record: staff.StaffSnapshot, slug: str, document: str) -> staff.StaffRow | None:
    """A timer names a staff path or a unique showrunner short name, including nested owners."""
    name = showrunners.space_from_text(slug, document)
    row = record.by_path(name)
    if row is not None and row.kind == "showrunner":
        return row
    owners = [owner for owner in record.find(kind="showrunner") if owner.name == name]
    return owners[0] if len(owners) == 1 else None


@dataclass(frozen=True)
class StaffLabels:
    record: staff.StaffSnapshot
    productions: Mapping[str, showrunners.ShowrunnerSource]
    showrunners: Mapping[str, staff.StaffRow]

    @classmethod
    def read(cls) -> StaffLabels:
        record = staff.snapshot()
        sources = showrunners.read_sources(unreadable=showrunners.UnreadableTimerPolicy.SKIP)
        productions = {source.timer.slug: source for source in sources}
        parents = {slug: row for slug, source in productions.items()
                   if (row := showrunner_owner(record, slug, source.document)) is not None}
        return cls(record, productions, parents)

    def environment(self, environment: Mapping[str, str], session_id: str, name: str = "", cwd: str = "") -> dict[str, str]:
        """Join identity, timer target, exact local worktree, then normalized name."""
        result = dict(environment)
        identity = result.get("STAFF_ID", "")
        row = self.record.by_id(staff.StaffId(identity)) if identity else None
        if identity and row is None:
            return result
        if row is None:
            owners: list[staff.StaffRow] = []
            for slug, source in self.productions.items():
                if not session_id or source.timer.target != f"session:{session_id}":
                    continue
                candidates = [owner for owner in self.record.rows
                              if owner.kind == "showrunner" and isinstance(owner.doc, staff.LiveDoc)
                              and owner.doc.machine == remote.local_machine() and owner.doc.path == source.timer.doc]
                if not candidates:
                    candidates = [owner for owner in self.record.rows if owner.kind == "showrunner" and owner.name == slug]
                if len(candidates) == 1:
                    owners.extend(candidates)
            if len(owners) == 1:
                row = owners[0]
        if row is None and cwd:
            owners = [owner for owner in self.record.rows
                      if owner.kind == "unit" and isinstance(owner.worktree, staff.PresentWorktree)
                      and owner.worktree.machine == remote.local_machine() and str(owner.worktree.path) == cwd]
            if len(owners) == 1:
                row = owners[0]
        if row is None:
            given = result.get("SHOWRUNNER_UNIT_ID") or name
            try:
                short = unit_names.name(given) if given else ""
            except unit_names.BadName:
                short = ""
            owners = [owner for owner in self.record.rows
                      if owner.kind in ("unit", "showrunner") and owner.name == short]
            if len(owners) == 1:
                row = owners[0]
        if row is None:
            return result
        result["STAFF_ID"] = row.id
        if row.kind == "showrunner":
            _ = result.pop("SHOWRUNNER_UNIT", None)
            _ = result.pop("SHOWRUNNER_UNIT_ID", None)
        elif row.kind == "unit":
            parent = self.record.by_id(row.parent) if row.parent is not None else None
            if parent is None or parent.kind != "showrunner":
                return result
            slugs = [slug for slug, owner in self.showrunners.items() if owner.id == parent.id]
            slug = slugs[0] if len(slugs) == 1 else parent.name
            result["SHOWRUNNER_UNIT"] = slug
            retained_id = result.get("SHOWRUNNER_UNIT_ID", "")
            try:
                retained_owner = unit_names.name(retained_id) if retained_id else ""
            except unit_names.BadName:
                retained_owner = ""
            if retained_owner == row.name:
                result["SHOWRUNNER_UNIT_ID"] = retained_id
            else:
                result["SHOWRUNNER_UNIT_ID"] = unit_names.unit_id(row.name)
        return result
