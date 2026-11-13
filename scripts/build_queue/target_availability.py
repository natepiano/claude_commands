"""Name each target block and hold, and why a natedev step does not go to the Mac.

Every fact comes from one `targets.target_snapshots()` reading and the
`build_targets.step_policy` reader. This module reads no block, hold, claim or
backoff file itself, and it runs no remote command.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, final

from scripts.build_queue.admission_records import PackageNotRecorded
from scripts.build_targets import build_targets, targets
from scripts.build_targets.target_config import ConfigError, ExcludedTarget, SshTarget
from scripts.message import unit_labels

if TYPE_CHECKING:
    from scripts.mac_test import mac_test


class TargetRow(TypedDict):
    target: str
    state: str
    reason: str
    holder: str
    unit: str
    showrunner: str
    since: str
    until: str


@dataclass(frozen=True)
class SnapshotsUnreadable:
    reason: str


Snapshots = tuple[targets.TargetSnapshot, ...] | SnapshotsUnreadable


def read_snapshots() -> Snapshots:
    """Read every target's coordination state once for one page reading."""
    try:
        return targets.target_snapshots()
    except (ConfigError, OSError, UnicodeError) as error:
        return SnapshotsUnreadable(str(error))


def holder_sessions(snapshots: Snapshots) -> list[str]:
    """Return the sessions that own a hold or block, for the page's one label pass."""
    if isinstance(snapshots, SnapshotsUnreadable):
        return []
    return [state.holder_session.session_id for snapshot in snapshots for state in snapshot.states
            if isinstance(state, targets.Held) and isinstance(state.holder_session, targets.HolderSession)]


def holder_labels(state: targets.Held, labels: Mapping[str, unit_labels.UnitLabels]) -> unit_labels.UnitLabels:
    session = state.holder_session
    if isinstance(session, targets.HolderSession) and session.session_id in labels:
        return labels[session.session_id]
    return unit_labels.UnitLabels(state.holder)


def availability_rows(snapshots: Snapshots, labels: Mapping[str, unit_labels.UnitLabels],
                      zone: tzinfo) -> list[TargetRow]:
    """One row for each block, hold or unreadable target; a free or busy target has none."""
    if isinstance(snapshots, SnapshotsUnreadable):
        return [unavailable_row("targets", snapshots.reason)]
    rows: list[TargetRow] = []
    for snapshot in snapshots:
        name = snapshot.target.name
        for state in snapshot.states:
            if isinstance(state, targets.Held):
                identity = holder_labels(state, labels)
                rows.append({"target": name, "state": "Blocked" if isinstance(snapshot.target, SshTarget) else "Held",
                             "reason": state.reason, "holder": state.holder, "unit": identity.unit,
                             "showrunner": identity.showrunner,
                             "since": targets.start_text(state.since, zone), "until": targets.end_text(state.end, zone)})
            elif isinstance(state, (targets.Unreachable, targets.StateUnreadable)):
                rows.append(unavailable_row(name, state.reason))
    return rows


def unavailable_row(target: str, reason: str) -> TargetRow:
    return {"target": target, "state": "Unavailable", "reason": reason, "holder": "", "unit": "", "showrunner": "",
            "since": "", "until": ""}


DYNAMIC_REASONS = frozenset({"backoff", "state"})


@dataclass(frozen=True)
class NoMacTarget:
    """No configured target runs macOS."""


@final
class MacRoutes:
    """Explain, for each natedev step, why it does not run on the Mac."""

    def __init__(self, snapshots: Snapshots, labels: Mapping[str, unit_labels.UnitLabels], zone: tzinfo) -> None:
        self.mac: targets.TargetSnapshot | NoMacTarget | SnapshotsUnreadable = (
            snapshots if isinstance(snapshots, SnapshotsUnreadable)
            else next((snapshot for snapshot in snapshots if snapshot.target.platform == "macos"), NoMacTarget()))
        self.labels: Mapping[str, unit_labels.UnitLabels] = labels
        self.zone: tzinfo = zone
        self.policies: dict[tuple[str, str | PackageNotRecorded, str], str] = {}

    def line(self, step: str, folder: str, package: str | PackageNotRecorded) -> str:
        mac = self.mac
        if isinstance(mac, SnapshotsUnreadable):
            return "Mac: state unreadable, " + mac.reason
        if isinstance(mac, NoMacTarget):
            return ""
        kind = build_targets.recorded_step_kind(step)
        if isinstance(kind, build_targets.KindNotOffloaded):
            return "Mac: kind not offloaded"
        rule = build_targets.step_routing_rule(kind)
        if isinstance(rule, build_targets.NatedevOnly):
            return "Mac: natedev only, " + rule.reason
        excluded = self.policy(mac, folder, package, kind)
        if excluded:
            return excluded
        return self.state_line(mac.states[0])

    def policy(self, mac: targets.TargetSnapshot, folder: str, package: str | PackageNotRecorded,
               kind: build_targets.StepKind) -> str:
        """Read each repository, package and kind once in a page reading.

        Backoff and unreadable state are not policy: the Mac state line names them.
        """
        key = (folder, package, kind)
        if key not in self.policies:
            name = package if isinstance(package, str) else ""
            try:
                result = build_targets.step_policy(mac.target, Path(folder), name, kind, backoff=mac.backoff)
                text = ""
                if isinstance(result, ExcludedTarget) and result.reason not in DYNAMIC_REASONS:
                    text = "Mac: excluded, " + result.reason
                    if isinstance(package, PackageNotRecorded) and result.reason == "linux_only":
                        text += "; package not recorded"
            except (ConfigError, OSError, UnicodeError, ValueError) as error:
                text = f"Mac: state unreadable, {error}"
            self.policies[key] = text
        return self.policies[key]

    def blocked(self) -> bool:
        """The Mac card shows the block, so the idle alert omits its copy."""
        return isinstance(self.mac, targets.TargetSnapshot) and isinstance(self.mac.states[0], targets.Held)

    def state_line(self, state: targets.TargetState) -> str:
        if isinstance(state, targets.Held):
            identity = holder_labels(state, self.labels)
            owner = identity.unit if identity.unit != "—" else identity.session
            if identity.showrunner != "—":
                owner += f" ({identity.showrunner})"
            return f"Mac: blocked by {owner} {targets.end_text(state.end, self.zone)}"
        if isinstance(state, targets.Running):
            return "Mac: busy with " + "; ".join(job.what for job in state.jobs)
        if isinstance(state, targets.Unreachable):
            return "Mac: unreachable, " + state.reason
        if isinstance(state, targets.StateUnreadable):
            return "Mac: state unreadable, " + state.reason
        return "Mac: no recorded block or claim; availability not checked"


def mac_claims(snapshots: Snapshots) -> Sequence[mac_test.HeldClaim]:
    """The Mac claims from the same reading, for the Mac rows' owner labels."""
    if isinstance(snapshots, SnapshotsUnreadable):
        return ()
    return next((snapshot.claims for snapshot in snapshots if snapshot.target.platform == "macos"), ())
