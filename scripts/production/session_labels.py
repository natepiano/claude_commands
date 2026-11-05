"""Resolve a conversation's session labels from its current staff owner."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from scripts.production import staff, showrunners
from scripts.lib import legacy_identity


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
        result = legacy_identity.without_legacy_identity(environment)
        row = self.record.by_current_session(session_id) if session_id else None
        if row is None:
            return result
        result["CLAUDE_CODE_SESSION_ID"] = row.current_session
        return result
