"""Resolve a conversation's session labels from its current staff owner."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from scripts.production import staff, showrunners, unit_names
from scripts.production.unit_table import unit_spellings


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
        return cls.for_record(staff.snapshot())

    @classmethod
    def for_record(cls, record: staff.StaffSnapshot) -> StaffLabels:
        """Join session labels against the report's single staff snapshot."""
        sources = showrunners.read_sources(unreadable=showrunners.UnreadableTimerPolicy.SKIP)
        productions = {source.timer.slug: source for source in sources}
        parents: dict[str, staff.StaffRow] = {}
        for row in record.find(kind="showrunner"):
            timer = showrunners.timer_for(row, record, sources=sources)
            if isinstance(timer, showrunners.Timer):
                parents[timer.slug] = row
        return cls(record, productions, parents)

    def environment(self, environment: Mapping[str, str], session_id: str) -> dict[str, str]:
        """Join the current native runtime identity without inferring a member."""
        result = {key: value for key, value in environment.items() if key != "STAFF_ID"}
        row = self.record.by_current_session(session_id) if session_id else None
        if row is None:
            return result
        result["CLAUDE_CODE_SESSION_ID"] = row.current_session
        if row.kind in ("showrunner", "agent"):
            _ = result.pop("SHOWRUNNER_UNIT", None)
            _ = result.pop("SHOWRUNNER_UNIT_ID", None)
        elif row.kind == "unit":
            parent = self.record.by_id(row.parent.id) if isinstance(row.parent, staff.ReportsTo) else None
            if parent is None or parent.kind != "showrunner":
                return result
            slugs = [slug for slug, owner in self.showrunners.items() if owner.id == parent.id]
            slug = slugs[0] if len(slugs) == 1 else parent.name
            result["SHOWRUNNER_UNIT"] = slug
            source = self.productions.get(slug)
            spellings = unit_spellings(source.document.splitlines()) if source is not None else {}
            retained_id = result.get("SHOWRUNNER_UNIT_ID", "")
            try:
                retained_owner = unit_names.name(retained_id) if retained_id else ""
            except unit_names.BadName:
                retained_owner = ""
            result["SHOWRUNNER_UNIT_ID"] = spellings.get(
                row.name, retained_id if retained_owner == row.name else unit_names.unit_id(row.name))
        return result
