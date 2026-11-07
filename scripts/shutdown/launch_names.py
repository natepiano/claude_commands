"""Resolve launch names and labels from one snapshot per operation."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING


from scripts.shutdown import pane_record  # noqa: E402
from scripts.herdr import pane_launch  # noqa: E402
from scripts.herdr.pane_launch import PaneLaunch  # noqa: E402

if TYPE_CHECKING:
    from scripts.production import session_staff, staff
    from scripts.production.session_labels import StaffLabels


@dataclass(frozen=True)
class SavedLaunchNames:
    """A command preview keeps saved names without a launch-name lookup."""


@dataclass(frozen=True)
class LaunchNamesNotRead:
    """The restore operation has not read its staff names."""


@dataclass(frozen=True)
class StaffNamesUnavailable:
    """The staff owner cannot supply names for this launch operation."""

    reason: str


@dataclass(frozen=True)
class LaunchNames:
    """Resolve launch names against one staff snapshot."""

    record: staff.StaffSnapshot | StaffNamesUnavailable

    @property
    def tab_record(self) -> staff.StaffSnapshot:
        """Keep tab ordering on this snapshot when the staff owner is unavailable."""
        from scripts.production import staff

        return staff.StaffSnapshot(()) if isinstance(self.record, StaffNamesUnavailable) else self.record

    @cached_property
    def membership(self) -> session_staff.LaunchMembership | StaffNamesUnavailable:
        """Read label sources once and retain saved labels when the sources are unavailable."""
        from scripts.production import session_staff, staff

        if isinstance(self.record, StaffNamesUnavailable):
            return self.record
        try:
            return session_staff.LaunchMembership(self.record, session_staff.member_sources())
        except (staff.StaffUnreadable, OSError, ValueError) as error:
            print(f"launch names: session labels are unavailable: {error}; saved labels retained", file=sys.stderr)
            return StaffNamesUnavailable(str(error))

    def environment(self, entry: PaneLaunch) -> dict[str, str]:
        """Resolve session labels without another staff or source read."""
        from scripts.production import session_staff

        restored = entry.copy()
        restored["environment"] = pane_record.retained_environment(entry, self.history)
        membership = self.membership
        if isinstance(membership, session_staff.LaunchMembership):
            return membership.environment(restored)
        return restored["environment"]

    @cached_property
    def history(self) -> tuple[PaneLaunch, ...]:
        """Read retained label evidence once for this operation."""
        return tuple(pane_record.launches())

    @cached_property
    def labels(self) -> StaffLabels | pane_launch.StaffLabelsRead:
        """Share the operation's label sources with tab ordering."""
        from scripts.production import session_staff
        from scripts.production.session_labels import StaffLabels

        membership = self.membership
        if isinstance(membership, session_staff.LaunchMembership):
            return StaffLabels(membership.record)
        return pane_launch.StaffLabelsRead.UNAVAILABLE

    @classmethod
    def read(cls) -> LaunchNames:
        from scripts.production import staff

        try:
            return cls(staff.initial_snapshot())
        except (staff.StaffUnreadable, OSError, ValueError) as error:
            print(f"launch names: staff is unavailable: {error}; saved names retained", file=sys.stderr)
            return cls(StaffNamesUnavailable(str(error)))

    def name(self, session_id: str, saved: str) -> str:
        """Resolve an observed native conversation without a name lookup."""
        if not session_id or isinstance(self.record, StaffNamesUnavailable):
            return saved
        owner = self.record.by_current_session(session_id)
        if owner is None:
            print(f"launch names: {session_id} ({saved}): no staff row; saved name retained", file=sys.stderr)
            return saved
        return owner.name

    def resume(self, entry: PaneLaunch) -> PaneLaunch:
        """Project a saved member reference to its current conversation and name."""
        current = pane_record.without_legacy_identity(entry)
        if isinstance(self.record, StaffNamesUnavailable):
            return current
        owner = self.record.by_member_reference(entry["session_id"])
        if owner is None:
            print(f"launch names: {entry['session_id']} ({entry['name']}): no staff row; saved name retained",
                  file=sys.stderr)
            return current
        current["session_id"] = owner.current_session
        current["name"] = owner.name
        from scripts.production import staff

        unit = self.record.parent_of(owner) if owner.kind == "worker" else owner
        if unit is not None and unit.kind == "unit" and not isinstance(unit.worktree, staff.WorktreeUnfilled):
            current["cwd"] = str(unit.worktree.path)
        return current

    def resume_proof(self, original: PaneLaunch, current: PaneLaunch) -> pane_launch.VerifiedResume:
        """Accept the fixed member reference and the observed native resume identity."""
        references = {original["session_id"], current["session_id"]}
        if not isinstance(self.record, StaffNamesUnavailable):
            owner = self.record.by_current_session(current["session_id"])
            if owner is not None:
                references.add(owner.id)
        return pane_launch.VerifiedResume(current, frozenset(references))

