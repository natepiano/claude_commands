"""Resolve a conversation's session labels from its current staff owner."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from scripts.production import staff, showrunners, unit_names
from scripts.production.unit_table import Refusal, cell_value, unit_table


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
        parents = {slug: row for slug, source in productions.items()
                   if (row := showrunner_owner(record, slug, source.document)) is not None}
        return cls(record, productions, parents)

    def environment(self, environment: Mapping[str, str], session_id: str, name: str = "", cwd: str = "") -> dict[str, str]:
        """Join the current native runtime identity without inferring a member."""
        del name, cwd
        result = {key: value for key, value in environment.items() if key != "STAFF_ID"}
        row = self.record.by_current_session(session_id) if session_id else None
        if row is None:
            return result
        result["CLAUDE_CODE_SESSION_ID"] = row.current_session
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
            identifiers: set[str] = set()
            source = self.productions.get(slug)
            if source is not None:
                try:
                    identifiers = {cell_value(cells["Unit"]) for cells in unit_table(source.document.splitlines())
                                   if "Unit" in cells and unit_names.name(cell_value(cells["Unit"])) == row.name}
                except (Refusal, unit_names.BadName):
                    pass
            retained_id = result.get("SHOWRUNNER_UNIT_ID", "")
            try:
                retained_owner = unit_names.name(retained_id) if retained_id else ""
            except unit_names.BadName:
                retained_owner = ""
            # A missing production table cannot rename an existing unit id.
            if len(identifiers) == 1:
                result["SHOWRUNNER_UNIT_ID"] = next(iter(identifiers))
            elif retained_owner == row.name:
                result["SHOWRUNNER_UNIT_ID"] = retained_id
            else:
                result["SHOWRUNNER_UNIT_ID"] = unit_names.unit_id(row.name)
        return result
