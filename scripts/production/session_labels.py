"""Resolve a conversation's session labels from its current staff owner."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from scripts.production import staff, showrunners
from scripts.lib import legacy_identity


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
            if isinstance(timer, showrunners.TimerRegistration):
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
