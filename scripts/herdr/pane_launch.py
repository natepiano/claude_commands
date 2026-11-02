"""Read saved session labels without loading a terminal or lifecycle controller."""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast
from enum import Enum

if TYPE_CHECKING:
    from scripts.production.session_labels import StaffLabels

import sessions
from place import HerdrPlace

UNIT_MARKS = ("SHOWRUNNER_UNIT", "SHOWRUNNER_UNIT_ID")
MEMBER_MARKS = UNIT_MARKS


class PaneLaunch(TypedDict):
    pane_id: str
    session_id: str
    config_dir: str
    cwd: str
    name: str
    model: str
    remote_control: bool
    environment: dict[str, str]
    # False when the launch flags could not be read: a restore refuses it.
    flags_read: bool
    ended: NotRequired[bool]
    written_at: str
    pid: NotRequired[int]
    proc_start: NotRequired[str]


def record_directory() -> Path:
    return Path(os.environ.get("HERDR_PANE_RECORD_DIR") or Path.home() / ".local/state/herdr-panes")


def read_entries(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path}: pane records must be an object")
    return cast(dict[str, object], value)


def read(path: Path) -> dict[str, PaneLaunch]:
    try:
        return cast(dict[str, PaneLaunch], read_entries(path))
    except (OSError, ValueError):
        return {}


def matching_launch(entries: Mapping[str, PaneLaunch], place: HerdrPlace, session_id: str) -> PaneLaunch | None:
    entry = entries.get(place.pane)
    if entry is None or entry.get("pane_id") != place.pane or entry.get("session_id") != session_id:
        return None
    return entry


class StaffLabelsRead(Enum):
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


class RecordedLabels:
    """One discovery read: each terminal's launch file is opened at most once."""

    def __init__(self, membership: StaffLabels | StaffLabelsRead = StaffLabelsRead.PENDING) -> None:
        self.entries: dict[str, dict[str, PaneLaunch]] = {}
        self.membership: StaffLabels | StaffLabelsRead = membership

    def resolve(self, pid: int, environment: Mapping[str, str],
                placement: Callable[[], object]) -> dict[str, str]:
        # Raw discovery already has these process labels. Ownership projections
        # pass their staff snapshot explicitly and still require native proof.
        if isinstance(self.membership, StaffLabelsRead) and all(environment.get(key) for key in UNIT_MARKS):
            return {key: value for key, value in environment.items() if key != "STAFF_ID"}
        record = sessions.read_session(sessions.sessions_dir() / f"{pid}.json")
        if (isinstance(record, sessions.UnreadableSessionRecord) or record["pid"] != pid
                or not sessions.running_session(record)):
            return dict(environment)
        if self.membership is StaffLabelsRead.PENDING:
            from scripts.production import staff
            from scripts.production.session_labels import StaffLabels

            try:
                self.membership = StaffLabels.read()
            except (staff.StaffUnreadable, OSError, ValueError):
                self.membership = StaffLabelsRead.UNAVAILABLE
        values = dict(environment)
        place = placement()
        if isinstance(place, HerdrPlace):
            if place.session not in self.entries:
                self.entries[place.session] = read(record_directory() / f"{place.session}.json")
            entries = self.entries[place.session]
            entry = matching_launch(entries, place, record["sessionId"])
            if entry is None and not isinstance(self.membership, StaffLabelsRead):
                owner = self.membership.record.by_current_session(record["sessionId"])
                if owner is not None:
                    entry = matching_launch(entries, place, owner.id)
            if entry is not None:
                values = {**{key: value for key, value in entry.get("environment", {}).items()
                             if key in MEMBER_MARKS}, **values}
        if not isinstance(self.membership, StaffLabelsRead):
            values = self.membership.environment(values, record["sessionId"], record["name"], record.get("cwd", ""))
        return values
