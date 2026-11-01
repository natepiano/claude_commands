"""Matching a running session to its staff row."""
from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

SCRIPTS = Path(__file__).resolve().parents[1]
for directory in (SCRIPTS / "message", SCRIPTS / "production", SCRIPTS / "herdr", SCRIPTS.parent):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
from scripts.production import staff, showrunners
from scripts.production.showrunners import canonical_slug
from sessions import SessionRecord
from place import HerdrPlace
from pane_launch import RecordedLabels
from scripts.production.session_labels import StaffLabels, showrunner_owner
import unit_names
if TYPE_CHECKING:
    from scripts.shutdown import pane_record


PRODUCTION_MARK = "SHOWRUNNER_UNIT"


UNIT_MARK = "SHOWRUNNER_UNIT_ID"


@dataclass(frozen=True)
class ShowrunnerMember:
    slug: str


@dataclass(frozen=True)
class UnitMember:
    slug: str
    unit: str


@dataclass(frozen=True)
class ObservedUnit:
    """A pane identity, independent of its launch-time production mark."""

    unit: str
    staff_id: str = ""


MemberSelector = ShowrunnerMember | UnitMember | ObservedUnit


@dataclass(frozen=True)
class MemberFound:
    row: staff.StaffRow


@dataclass(frozen=True)
class NoSuchMember:
    selector: MemberSelector


def unit_match(selector: UnitMember | ObservedUnit, record: staff.StaffSnapshot,
                staff_id: str = "", *, parent: staff.StaffRow | None = None) -> MemberFound | NoSuchMember:
    """One id-first join; production requests also require their current parent."""
    if staff_id:
        row = record.by_id(staff.StaffId(staff_id))
        matches = [row] if row is not None and row.kind == "unit" else []
    else:
        matches = [row for row in record.find(kind="unit") if row.name == unit_names.name(selector.unit)]
    if isinstance(selector, UnitMember):
        matches = [row for row in matches if parent is not None and row.parent == parent.id]
    if len(matches) > 1:
        raise staff.StaffUnreadable(f"{selector.unit}: no unique staff entry")
    return MemberFound(matches[0]) if matches else NoSuchMember(selector)


@dataclass(frozen=True)
class MemberSources:
    productions: dict[str, showrunners.ShowrunnerSource]
    launches: tuple[pane_record.PaneLaunch, ...]

    def member(self, selector: MemberSelector, record: staff.StaffSnapshot) -> MemberFound | NoSuchMember:
        if isinstance(selector, ObservedUnit):
            return unit_match(selector, record, selector.staff_id)
        source = self.productions.get(selector.slug)
        if isinstance(selector, UnitMember):
            parent = self.member(ShowrunnerMember(selector.slug), record)
            if isinstance(parent, NoSuchMember):
                return NoSuchMember(selector)
            # A transferred launch keeps its old production mark. Only its row's
            # current parent admits it to this request; timestamps cannot choose
            # between identities from different productions.
            launches = [launch for launch in self.launches
                        if launch["environment"].get(UNIT_MARK) == selector.unit
                        and launch["environment"].get("STAFF_ID")]
            local = [launch for launch in launches
                     if launch["environment"].get(PRODUCTION_MARK) == selector.slug]
            if local:
                latest = max(local, key=lambda launch: launch["written_at"])
                found = unit_match(selector, record, latest["environment"]["STAFF_ID"], parent=parent.row)
                if isinstance(found, MemberFound):
                    return found
                # A missing durable id is not permission to reuse its old name.
                if (identity := record.by_id(staff.StaffId(latest["environment"]["STAFF_ID"]))) is None or identity.kind != "unit":
                    return NoSuchMember(selector)
            identities: dict[staff.StaffId, MemberFound] = {}
            for launch in launches:
                found = unit_match(selector, record, launch["environment"]["STAFF_ID"], parent=parent.row)
                if isinstance(found, MemberFound):
                    identities[found.row.id] = found
            if len(identities) > 1:
                raise staff.StaffUnreadable(f"{selector.slug}/{selector.unit}: conflicting staff identities")
            if identities:
                return next(iter(identities.values()))
            return unit_match(selector, record, parent=parent.row)
        if source is None:
            row = record.by_path(selector.slug)
            return MemberFound(row) if row is not None and row.kind == "showrunner" else NoSuchMember(selector)
        launches = [launch for launch in self.launches
                    if f"session:{launch['session_id']}" == source.timer.target]
        staff_id = max(launches, key=lambda launch: launch["written_at"])["environment"].get("STAFF_ID", "") if launches else ""
        if staff_id:
            row = record.by_id(staff.StaffId(staff_id))
            return MemberFound(row) if row is not None and row.kind == "showrunner" else NoSuchMember(selector)
        row = showrunner_owner(record, selector.slug, source.document)
        return MemberFound(row) if row is not None and row.kind == "showrunner" else NoSuchMember(selector)

    def showrunner_members(self, record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
        found: dict[str, staff.StaffRow] = {}
        for slug in self.productions:
            selected = self.member(ShowrunnerMember(slug), record)
            if isinstance(selected, MemberFound):
                found[slug] = selected.row
        return found


def member_sources(*, dependencies: Callable[[Path], None] | None = None,
                   unreadable: showrunners.UnreadableTimerPolicy = showrunners.UnreadableTimerPolicy.SKIP) -> MemberSources:
    import herdr
    from scripts.shutdown import pane_record

    session = herdr.shared_session(dependencies=dependencies)
    try:
        launches = pane_record.read_strict(session, dependencies=dependencies)
    except OSError as error:
        raise staff.StaffUnreadable(f"{pane_record.record_path(session)}: {error}") from error
    except ValueError as error:
        raise staff.StaffUnreadable(str(error)) from error
    sources = showrunners.read_sources(unreadable=unreadable,
                                       dependencies=dependencies)
    return MemberSources({source.timer.slug: source for source in sources}, tuple(launches.values()))


class LiveClaude(NamedTuple):
    name: str
    session_id: str
    socket: str
    pid: int


class ClaudeNotRunning(NamedTuple):
    pass


class ClaudeUnknown(NamedTuple):
    """The session records could not all be read, which says nothing about whether Claude runs."""

    reason: str


Claude = LiveClaude | ClaudeNotRunning | ClaudeUnknown


Place = HerdrPlace


@dataclass(frozen=True)
class MarkedUnit:
    unit: str
    claude: Claude
    place: Place
    pid: int = 0
    command: str = ""
    staff_id: str = ""


def place_text(place: Place) -> str:
    return f"herdr:{place.session}:{place.pane}"


def holder_text(unit: MarkedUnit) -> str:
    """Identify the process whose marks caused a refusal, including uncertain holders."""
    pid = unit.pid or (unit.claude.pid if isinstance(unit.claude, LiveClaude) else 0)
    command = unit.command or ("claude" if isinstance(unit.claude, LiveClaude) else "unknown")
    return f"pid {pid} ({command}) in {place_text(unit.place)}"


@dataclass(frozen=True)
class MarkedProductions:
    # Each production's marked units, by production slug and then unit id.
    units: dict[str, dict[str, MarkedUnit]]
    # Why a unit was left out, by production slug and then unit id: two sessions carry its mark.
    faults: dict[str, dict[str, str]]
    members: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False)

    def production_fault(self, slug: str) -> str:
        """Every left-out unit of the production, in one line; empty when none was left out."""
        return "; ".join(detail for _, detail in sorted(self.faults.get(slug, {}).items()))


def lookup_slug(slug: str) -> str:
    # A current notifier name needs no scan of Formerly entries or reread of its doc.
    if (showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}").is_dir():
        return slug
    return canonical_slug(slug)


def production_members(record: staff.StaffSnapshot, sources: MemberSources) -> dict[str, tuple[staff.StaffRow, ...]]:
    """All recorded unit children, including those with no pane or local worktree."""
    active_runners = {row.id for row in staff.active(record, "showrunner")}
    runners = {slug: row for slug, row in sources.showrunner_members(record).items() if row.id in active_runners}
    # A timer is not required for an offline production whose short name is its slug.
    mapped = {row.id for row in runners.values()}
    runners.update({row.name: row for row in staff.active(record, "showrunner")
                    if row.id not in mapped and row.name not in runners})
    return {slug: tuple(unit for unit in staff.active(record, "unit") if unit.parent == row.id)
            for slug, row in runners.items()}


def group_marked_units(marked: Iterable[tuple[str, MarkedUnit]], *,
                       record: staff.StaffSnapshot | None = None,
                       sources: MemberSources | None = None) -> MarkedProductions:
    """Join panes to the recorded parent; refuse ambiguous legacy names and duplicate panes."""
    record = record if record is not None else staff.snapshot()
    sources = sources if sources is not None else member_sources()
    return _group_marked_units(marked, record, sources, production_members(record, sources))


def retained_unit_marks(marked: Iterable[tuple[str, MarkedUnit]], row: staff.StaffRow, slug: str, *,
                        record: staff.StaffSnapshot, sources: MemberSources) -> MarkedProductions:
    """Keep pane evidence for a retained identity during stop and cleanup checks."""
    membership = production_members(record, sources)
    if row not in membership.get(slug, ()):
        membership[slug] = (*membership.get(slug, ()), row)
    return _group_marked_units(marked, record, sources, membership)


def _group_marked_units(marked: Iterable[tuple[str, MarkedUnit]], record: staff.StaffSnapshot,
                        sources: MemberSources,
                        membership: dict[str, tuple[staff.StaffRow, ...]]) -> MarkedProductions:
    parents = {row.id: slug for slug, rows in membership.items() for row in rows}
    found: dict[str, dict[staff.StaffId, list[MarkedUnit]]] = {}
    faults: dict[str, dict[str, str]] = {}
    for old_slug, unit in marked:
        try:
            selected = sources.member(ObservedUnit(unit.unit, unit.staff_id), record)
            if isinstance(selected, NoSuchMember):
                continue
            row = selected.row
            slug = parents.get(row.id)
            if slug is None:
                parent = record.by_id(row.parent) if row.parent is not None else None
                if (isinstance(row.worktree, staff.RemovedWorktree)
                        or (parent is not None and parent.kind == "showrunner"
                            and isinstance(parent.doc, staff.WrappedDoc))):
                    continue
                raise staff.StaffUnreadable(f"{row.name}: no showrunner staff parent")
        except (staff.StaffUnreadable, unit_names.BadName) as error:
            faults.setdefault(old_slug, {})[unit.unit] = str(error)
            continue
        found.setdefault(slug, {}).setdefault(row.id, []).append(unit)
    units: dict[str, dict[str, MarkedUnit]] = {}
    names: dict[staff.StaffId, str] = {}
    for slug, production in found.items():
        units[slug] = {}
        for identity, places in production.items():
            row = record.by_id(identity)
            assert row is not None
            # Inherited marks preserve a stopped pane for recovery, but cannot
            # compete with a live Claude or unresolved session for ownership.
            holders = [place for place in places if isinstance(place.claude, (LiveClaude, ClaudeUnknown))]
            selected = holders[0] if holders else places[0]
            name = (selected.unit if selected.unit and unit_names.name(selected.unit) == row.name
                    else unit_names.unit_id(row.name))
            names[identity] = name
            if len(holders) > 1:
                sessions = " and ".join(sorted(holder_text(place) for place in holders))
                faults.setdefault(slug, {})[name] = f"{name}: its mark is on herdr sessions {sessions}"
            else:
                units[slug][name] = selected if selected.unit == name else replace(selected, unit=name)
    return MarkedProductions(units, faults, {slug: tuple(names.get(row.id, unit_names.unit_id(row.name)) for row in rows)
                                           for slug, rows in membership.items()})


def session_staff_and_directors(
    records: list[SessionRecord], environment: Mapping[str, str], *, record: staff.StaffSnapshot | None = None,
) -> tuple[MarkedProductions, dict[str, str]]:
    """Match known session PIDs to staff and directors, without enumerating processes."""
    marked: list[tuple[str, MarkedUnit]] = []
    directors: dict[str, str] = {}
    known = {session["sessionId"] for session in records}
    proc = Path(environment.get("HERDR_PROC", "/proc"))
    record = record if record is not None else staff.snapshot()
    sources = member_sources()
    labels = RecordedLabels(StaffLabels(record, sources.productions, sources.showrunner_members(record)))
    for session in records:
        try:
            # Herdr's Linux environment reader is private; keep this PID-only IO here.
            raw = (proc / str(session["pid"]) / "environ").read_bytes()
            values = dict(item.split("=", 1) for item in raw.decode(errors="replace").split("\0")
                          if "=" in item)
        except OSError:
            continue
        owner = values.get("CLAUDE_CODE_SESSION_ID", "")
        if owner in known and owner != session["sessionId"]:
            directors[session["sessionId"]] = owner
            continue
        values = labels.resolve(session["pid"], values,
            lambda: HerdrPlace(values.get("HERDR_SESSION", ""), values.get("HERDR_PANE_ID", "")))
        slug = values.get(PRODUCTION_MARK, "")
        unit = values.get(UNIT_MARK, "")
        staff_id = values.get("STAFF_ID", "")
        if unit or staff_id:
            unit_mark = MarkedUnit(
                unit, LiveClaude(session["name"], session["sessionId"],
                                 session["messagingSocketPath"], session["pid"]),
                HerdrPlace(values.get("HERDR_SESSION", ""), values.get("HERDR_PANE_ID", "")),
                staff_id=staff_id,
            )
            marked.append((slug, unit_mark))
    return group_marked_units(marked, record=record, sources=sources), directors


@dataclass(frozen=True)
class LaunchMembership:
    """The staff owner's identities and the production documents that name their parents."""

    record: staff.StaffSnapshot
    sources: MemberSources

    @classmethod
    def read(cls) -> LaunchMembership:
        return cls(staff.snapshot(), member_sources())

    def environment(self, launch: pane_record.PaneLaunch, *, live_name: str = "") -> dict[str, str]:
        return StaffLabels(self.record, self.sources.productions,
                           self.sources.showrunner_members(self.record)).environment(
                               launch["environment"], launch["session_id"], live_name or launch["name"], launch["cwd"])


def record_membership(launch: pane_record.PaneLaunch) -> pane_record.PaneLaunch:
    """Supply launch session labels from its conversation history and staff owner."""
    from scripts.shutdown import pane_record

    result = launch.copy()
    result["environment"] = pane_record.retained_environment(result, pane_record.launches())
    try:
        result["environment"] = LaunchMembership.read().environment(result)
    except (staff.StaffUnreadable, OSError, ValueError):
        pass  # An unavailable owner cannot erase already recorded identity.
    return result


@dataclass(frozen=True)
class RepairedSessionLabels:
    identity: str
    assigned: tuple[tuple[str, str], ...]
    removed: tuple[str, ...]


def repair_marks() -> list[RepairedSessionLabels]:
    """Repair retained identities in place; never launch or change a conversation."""
    import sessions
    from scripts.shutdown import pane_record

    membership = LaunchMembership.read()
    history = pane_record.launches()
    newest = {entry["session_id"]: entry["written_at"]
              for entry in sorted(history, key=lambda item: item["written_at"])}
    live = sessions.running_sessions()
    running = {entry["sessionId"]: entry["name"] for entry in live}
    repaired: list[RepairedSessionLabels] = []
    if live:
        import herdr

        by_pid = {entry["pid"]: entry for entry in live}
        seen: set[str] = set()
        for process in herdr.herdr_processes():
            session = by_pid.get(process.pid)
            if session is None or session["sessionId"] in seen:
                continue
            seen.add(session["sessionId"])
            if pane_record.matching_launch(process.place, session["sessionId"]) is not None:
                continue
            try:
                entry = pane_record.running_launch(session, process.place, process.cwd)
            except (OSError, ValueError):
                continue
            entry["environment"] = membership.environment(entry, live_name=session["name"])
            if not entry["environment"].get("STAFF_ID"):
                continue
            pane_record.write(process.place.session, entry)
            newest[entry["session_id"]] = entry["written_at"]
            repaired.append(RepairedSessionLabels(
                f"{process.place.session}:{process.place.pane}:{entry['session_id']}",
                tuple((key, value) for key, value in sorted(entry["environment"].items())
                      if key in herdr.MEMBER_MARKS), ()))
    for path in sorted(pane_record.record_directory().glob("*.json")):
        with pane_record.locked(path.stem):
            entries = pane_record.read_strict(path.stem)
            changed = False
            for pane, entry in entries.items():
                if entry["written_at"] != newest.get(entry["session_id"]):
                    continue
                restored = entry.copy()
                restored["environment"] = pane_record.retained_environment(entry, history)
                restored["environment"] = membership.environment(
                    restored, live_name=running.get(entry["session_id"], ""))
                if restored != entry:
                    entries[pane] = restored
                    before, after = entry["environment"], restored["environment"]
                    repaired.append(RepairedSessionLabels(
                        f"{path.stem}:{pane}:{entry['session_id']}",
                        tuple((key, value) for key, value in sorted(after.items()) if before.get(key) != value),
                        tuple(sorted(key for key in before if key not in after))))
                    changed = True
            if changed:
                pane_record.save(path.stem, entries)
    return repaired


def main(arguments: list[str] | None = None) -> int:
    """Repair saved session labels without starting or stopping a process."""
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    _ = parser.add_argument("command", choices=["repair"])
    _ = parser.parse_args(arguments)
    try:
        repaired = repair_marks()
    except (staff.StaffUnreadable, OSError, ValueError) as error:
        print(f"session labels: {error}", file=sys.stderr)
        return 1
    for repair in repaired:
        changes = [f"set {key}={value}" for key, value in repair.assigned]
        changes.extend(f"removed {key}" for key in repair.removed)
        print(f"{repair.identity}: " + "; ".join(changes))
    if not repaired:
        print("nothing to repair")
    return 0


if __name__ == "__main__":
    sys.exit(main())
