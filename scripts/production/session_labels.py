"""Resolve a conversation's session labels from its current staff owner."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from scripts.production import staff
from scripts.lib import legacy_identity


@dataclass(frozen=True)
class StaffLabels:
    record: staff.StaffSnapshot

    @classmethod
    def read(cls) -> StaffLabels:
        return cls.for_record(staff.snapshot())

    @classmethod
    def for_record(cls, record: staff.StaffSnapshot) -> StaffLabels:
        """Join session labels against the report's single staff snapshot."""
        return cls(record)

    def environment(self, environment: Mapping[str, str], session_id: str) -> dict[str, str]:
        """Join the current native runtime identity without inferring a member."""
        result = legacy_identity.without_legacy_identity(environment)
        row = self.record.by_current_session(session_id) if session_id else None
        if row is None:
            return result
        result["CLAUDE_CODE_SESSION_ID"] = row.current_session
        return result
