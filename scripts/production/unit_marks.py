"""Staff identity joins and unit marks shared by file readers and process discovery."""
from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
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
        row = record.by_path(showrunners.space_from_text(selector.slug, source.document))
        return MemberFound(row) if row is not None and row.kind == "showrunner" else NoSuchMember(selector)

    def showrunner_members(self, record: staff.StaffSnapshot) -> dict[str, staff.StaffRow]:
        found: dict[str, staff.StaffRow] = {}
        for slug in self.productions:
            selected = self.member(ShowrunnerMember(slug), record)
            if isinstance(selected, MemberFound):
                found[slug] = selected.row
        return found


def member_sources() -> MemberSources:
    import herdr
    from scripts.shutdown import pane_record

    session = herdr.shared_session()
    try:
        launches = pane_record.read_strict(session)
    except OSError as error:
        raise staff.StaffUnreadable(f"{pane_record.record_path(session)}: {error}") from error
    except ValueError as error:
        raise staff.StaffUnreadable(str(error)) from error
    sources = showrunners.read_sources(unreadable=showrunners.UnreadableTimerPolicy.SKIP)
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
    runners = sources.showrunner_members(record)
    # A timer is not required for an offline production whose short name is its slug.
    mapped = {row.id for row in runners.values()}
    runners.update({row.name: row for row in record.find(kind="showrunner")
                    if row.id not in mapped and row.name not in runners})
    return {slug: tuple(record.find(kind="unit", parent=row.id)) for slug, row in runners.items()}


def group_marked_units(marked: Iterable[tuple[str, MarkedUnit]], *,
                       record: staff.StaffSnapshot | None = None) -> MarkedProductions:
    """Join panes to the recorded parent; refuse ambiguous legacy names and duplicate panes."""
    record = record if record is not None else staff.snapshot()
    sources = member_sources()
    membership = production_members(record, sources)
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


def session_unit_marks(
    records: list[SessionRecord], environment: Mapping[str, str], *, record: staff.StaffSnapshot | None = None,
) -> tuple[MarkedProductions, dict[str, str]]:
    """Read unit marks only for known session PIDs, without enumerating processes."""
    marked: list[tuple[str, MarkedUnit]] = []
    directors: dict[str, str] = {}
    known = {session["sessionId"] for session in records}
    proc = Path(environment.get("HERDR_PROC", "/proc"))
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
    return group_marked_units(marked, record=record), directors
