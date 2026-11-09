#!/usr/bin/env python3
"""Read local showrunner timers, resolve their addresses and send production notices."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import math
from time import monotonic
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import NamedTuple, TypedDict, cast, override


from scripts.lib.atomic_file import write_bytes_atomic

from scripts.lib import conf as tunables
from scripts.message import session_send

CONFIG = Path(os.environ.get("SHOWRUNNERS_CONFIG") or tunables.path("showrunners"))
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
SESSIONS = Path(os.environ.get("SHOWRUNNERS_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")
SHOWRUNNER_STATE_DIR = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
# Where Claude Code keeps each session scratchpad, as /tmp/claude-<uid>/<project>/<session id>/scratchpad.
SCRATCH_ROOT = Path(os.environ.get("SHOWRUNNER_SCRATCH_ROOT") or "/tmp")
SEND = Path(os.environ.get("SHOWRUNNERS_SEND") or session_send.SEND_SCRIPT)
PARENT_FILE = "parent"
from scripts.lib.user_zone import user_zone_name  # noqa: E402
from scripts.message.sessions import SessionRecord, messaging_socket_exists  # noqa: E402
from scripts.lib.session_paths import sessions_directory as sessions_directory
from scripts.production import staff  # noqa: E402
from scripts.agents import remote  # noqa: E402

RULES = "Read the Nested showrunners rules in ~/.claude/commands/showrunner/produce.md."


class Showrunner(TypedDict):
    """One showrunner, read from its update timer each time. Nothing here is stored in the config."""

    # The session's name now, looked up by its Claude session id. Empty while it is not running.
    session: str
    # Where a message reaches it now. Empty while it is not running.
    socket: str
    # The production name comes from the showrunner staff row.
    name: str
    # The user's zone setting. A production doc's own User zone line is no longer read.
    zone: str
    # The production doc. The staff rows whose parent is this showrunner are its units.
    doc: str


class ShowrunnerSettings(TypedDict):
    threshold_percent: float
    repeat_minutes: float
    stall_minutes: float
    faults_to: str
    always: list[str]


@dataclass(frozen=True)
class ShowrunnerLookupFailure:
    """A production whose session liveness could not be determined."""

    production: str
    reason: str

    @property
    def diagnostic(self) -> str:
        return f"showrunners: skipping showrunner-{self.production}: {self.reason}"


class ShowrunnerDiscovery(list[Showrunner]):
    """Healthy registrations together with the lookup failures reported during discovery."""

    lookup_failures: tuple[ShowrunnerLookupFailure, ...]

    @override
    def __init__(self, runners: Iterable[Showrunner] = (),
                 failures: tuple[ShowrunnerLookupFailure, ...] = ()) -> None:
        super().__init__(runners)
        self.lookup_failures = failures

    @property
    def failures(self) -> tuple[str, ...]:
        """The diagnostics reported during discovery, rendered from the lookup failures."""
        return tuple(failure.diagnostic for failure in self.lookup_failures)

    @property
    def exit_code(self) -> int:
        """Commands finish healthy work before returning a failure status."""
        return int(bool(self.lookup_failures))


class DiscoverMembership(NamedTuple):
    """Read staff when no report snapshot was supplied."""


DISCOVER_MEMBERSHIP = DiscoverMembership()


class Timer(NamedTuple):
    """One complete timer for a staff owner."""

    name: str
    target: str
    doc: Path
    zone: str


@dataclass(frozen=True)
class NoTimer:
    reason: str


class TimerRegistration(NamedTuple):
    """A staff owner's timer, independent of its production document."""

    name: str
    target: str
    zone: str


def _registration(instance: Path, record: staff.StaffSnapshot, *,
                  dependencies: Callable[[Path], None] | None = None) -> TimerRegistration:
    fields = _fields(instance / "conf", dependencies=dependencies)
    target = fields["TARGET"]
    if not target.startswith("session:") or not target[8:]:
        raise ValueError("invalid TARGET")
    owner = owner_for_timer(instance, record)
    return TimerRegistration(owner.name, f"session:{owner.id}", user_zone_name())


def timer_for(row: staff.StaffRow, snapshot: staff.StaffSnapshot) -> TimerRegistration | NoTimer:
    """Read the fixed owner's timer without requiring a document."""
    instance = NOTIFIER_STATE_DIR / f"showrunner-{row.id}"
    if instance.exists():
        return _registration(instance, snapshot)
    return NoTimer(f"{row.name}: no timer for staff {row.id}")


def owner_for(reference: str, record: staff.StaffSnapshot | None = None) -> staff.StaffRow:
    """Resolve a staff id or production label through the staff file."""
    record = record if record is not None else staff.snapshot()
    found = next((row for row in record.rows if row.kind == "showrunner"
                and (row.id == reference or row.name == reference)), None)
    if found is None:
        raise ValueError(f"{reference}: no showrunner staff row")
    return found


def timer_name(reference: str, record: staff.StaffSnapshot | None = None) -> str:
    """Return the timer folder name for the fixed staff owner."""
    return f"showrunner-{owner_for(reference, record).id}"


def owner_for_timer(instance: Path, record: staff.StaffSnapshot | None = None) -> staff.StaffRow:
    """Read the owner identified by the timer folder's staff id."""
    record = record if record is not None else staff.snapshot()
    owner = record.by_id(staff.StaffId(instance.name.removeprefix("showrunner-")))
    if owner is None or owner.kind != "showrunner":
        raise ValueError(f"{instance}: no showrunner staff row")
    return owner


@dataclass(frozen=True)
class ShowrunnerSource:
    """One readable timer and the production document read with it."""

    timer: Timer
    document: str


def load_settings() -> ShowrunnerSettings:
    return load_settings_from(CONFIG)


def load_settings_from(path: Path) -> ShowrunnerSettings:
    settings = tunables.read(path)
    return ShowrunnerSettings(
        threshold_percent=settings.number("threshold_percent"),
        repeat_minutes=settings.number("repeat_minutes"),
        stall_minutes=settings.number("stall_minutes"),
        faults_to=settings.text("faults_to"), always=settings.values()["always"].split())


def socket_for(target: str, *, deadline: float = math.inf) -> str | None:
    """The session's socket, or None when it is not running. Raises OSError when the session
    records cannot be read, which says nothing about whether it runs."""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{target}: session lookup deadline exhausted; retained for the next pass")
    try:
        result = subprocess.run([sys.executable, str(SESSIONS), "socket", target], capture_output=True,
                                text=True, check=False, timeout=remaining if math.isfinite(remaining) else None)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"{target}: session lookup timed out; retained for the next pass") from error
    # Exit 1 is no such live session. Any other failure is the lookup itself failing.
    if result.returncode not in (0, 1):
        reason = result.stderr.strip() or f"sessions.py exited {result.returncode}"
        raise OSError(f"cannot tell whether {target} is running: {reason}")
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _fields(path: Path, *, dependencies: Callable[[Path], None] | None = None) -> dict[str, str]:
    if dependencies is not None:
        dependencies(path)
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def session_name(session_id: str) -> str:
    """The live Claude session's name, or empty when it has none or is not running."""
    return _session_name(session_id)


def _session_name(session_id: str) -> str:
    for path in sessions_directory().glob("*.json"):
        try:
            pid = int(path.stem)
            try:
                os.kill(pid, 0)
            except PermissionError:
                # Another user's process is still a live one.
                pass
            data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
            name = data.get("name")
            if data.get("sessionId") == session_id and isinstance(name, str) and name:
                return name
        except (OSError, ValueError, TypeError):
            continue
    raise ValueError(f"session {session_id} has no live named process")


def _source(instance: Path, *, dependencies: Callable[[Path], None] | None = None,
            record: staff.StaffSnapshot | DiscoverMembership = DISCOVER_MEMBERSHIP) -> ShowrunnerSource:
    fields = _fields(instance / "conf", dependencies=dependencies)
    target = fields["TARGET"]
    if not target.startswith("session:") or not target[8:]:
        raise ValueError("invalid TARGET")
    record = staff.snapshot() if isinstance(record, DiscoverMembership) else record
    owner = owner_for_timer(instance, record)
    if not isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)):
        raise ValueError(f"{owner.name}: no production document")
    path = owner.doc.path
    if dependencies is not None:
        dependencies(path)
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"production doc could not be read: {error}") from error
    timer = Timer(owner.name, f"session:{owner.id}", path, user_zone_name())
    return ShowrunnerSource(timer, document)


class UnreadableTimerPolicy(Enum):
    """Whether unreadable timer evidence must stop discovery or may be skipped by seed."""

    RAISE = "raise"
    SKIP = "skip"
    STRICT = "strict"  # Audit every source, including incomplete timer declarations.


def _report_source_failure(failure: ShowrunnerLookupFailure) -> None:
    print(failure.diagnostic, file=sys.stderr)


def read_sources(state_dir: Path | None = None, *,
                 unreadable: UnreadableTimerPolicy,
                 dependencies: Callable[[Path], None] | None = None,
                 record: staff.StaffSnapshot | DiscoverMembership = DISCOVER_MEMBERSHIP,
                 on_failure: Callable[[ShowrunnerLookupFailure], None] = _report_source_failure,
                 ) -> list[ShowrunnerSource]:
    """Read each timer/doc once, applying the caller's policy to unreadable timers."""
    found: list[ShowrunnerSource] = []
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        if not instance.is_dir():
            continue
        try:
            found.append(_source(instance, dependencies=dependencies, record=record))
        except (OSError, KeyError, ValueError, UnicodeError) as error:
            if unreadable is UnreadableTimerPolicy.STRICT:
                raise ValueError(f"{instance / 'conf'}: {error}") from error
            if isinstance(error, OSError) and unreadable is UnreadableTimerPolicy.RAISE:
                raise
            on_failure(ShowrunnerLookupFailure(instance.name.removeprefix("showrunner-"), str(error)))
    return found


class DiscoverSessions(NamedTuple):
    """No registry snapshot was supplied; resolve sessions from their current records."""


DISCOVER_SESSIONS = DiscoverSessions()


def _registered(timer: TimerRegistration, *,
                known_sessions: Mapping[str, SessionRecord] | DiscoverSessions = DISCOVER_SESSIONS,
                deadline: float = math.inf,
                membership: staff.StaffSnapshot | None = None) -> Showrunner:
    membership = membership if membership is not None else staff.initial_snapshot()
    owner = membership.by_id(staff.StaffId(timer.target[8:]))
    current = owner.current_session if owner is not None else timer.target[8:]
    target = "session:" + current
    if isinstance(known_sessions, DiscoverSessions):
        socket = (socket_for(target, deadline=deadline) if math.isfinite(deadline)
                  else socket_for(target)) or ""
        name = _session_name(current) if socket else ""
    else:
        record = known_sessions.get(current)
        live = record is not None and messaging_socket_exists(record)
        socket = record["messagingSocketPath"] if record is not None and live else ""
        name = record["name"] if record is not None and live else ""
    doc = (str(owner.doc.path) if owner is not None
           and isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)) else "")
    return Showrunner(session=name, socket=socket or "", name=timer.name, zone=timer.zone, doc=doc)


def registered_showrunners(
    state_dir: Path | None = None, *,
    known_sessions: Mapping[str, SessionRecord] | DiscoverSessions = DISCOVER_SESSIONS,
    deadline: float = math.inf,
    membership: staff.StaffSnapshot | DiscoverMembership = DISCOVER_MEMBERSHIP,
) -> ShowrunnerDiscovery:
    """Read registered timers and resolve their current sessions through staff."""
    found: list[Showrunner] = []
    failures: list[ShowrunnerLookupFailure] = []
    membership = staff.initial_snapshot() if isinstance(membership, DiscoverMembership) else membership
    timers: list[TimerRegistration] = []
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        if not instance.is_dir():
            continue
        owner = membership.by_id(staff.StaffId(instance.name.removeprefix("showrunner-")))
        try:
            timers.append(_registration(instance, membership))
        except (KeyError, ValueError, UnicodeError) as error:
            name = owner.name if owner is not None else instance.name.removeprefix("showrunner-")
            _report_source_failure(ShowrunnerLookupFailure(name, str(error)))
    timers.sort(key=lambda timer: timer.name)
    for index, timer in enumerate(timers):
        try:
            began = monotonic()
            lookup_deadline = began + max(0.0, deadline - began) / (len(timers) - index + 1)
            found.append(_registered(timer, known_sessions=known_sessions, deadline=lookup_deadline, membership=membership))
        except (OSError, KeyError, ValueError, UnicodeError) as error:
            failure = ShowrunnerLookupFailure(timer.name, str(error))
            if isinstance(error, OSError):
                failures.append(failure)
            print(failure.diagnostic, file=sys.stderr)
    return ShowrunnerDiscovery(found, tuple(failures))


def production_of(session_id: str, state_dir: Path | None = None) -> Path | None:
    """Read the current session's production document from staff."""
    del state_dir
    owner = staff.snapshot().by_current_session(session_id)
    if (owner is not None and owner.kind == "showrunner"
            and isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
            and owner.doc.machine == remote.local_machine()):
        return owner.doc.path
    return None


def session_state_dir(staff_id: str) -> Path:
    """The showrunner's own state, which a new session id, a restart and a reboot keep; a wrap deletes it."""
    return SHOWRUNNER_STATE_DIR / staff_id / "session"


# The showrunner state a scratchpad held before session_state_dir.
SESSION_STATE = ("dailies_input_state", "unit_status", "showrunner_state.json", "dailies_judgment.json",
                 "dailies_state.json")


def copy_session_state(staff_id: str, scratch: Path) -> list[str]:
    """Copy missing scratchpad files to the fixed owner's session folder."""
    target = session_state_dir(staff_id)
    copied: list[str] = []
    for name in SESSION_STATE:
        source, destination = scratch / name, target / name
        if not source.exists():
            continue
        files = sorted(source.rglob("*")) if source.is_dir() else [source]
        changed = False
        for file in files:
            if not file.is_file():
                continue
            path = destination / file.relative_to(source) if source.is_dir() else destination
            if not path.exists():
                write_bytes_atomic(path, file.read_bytes())
                changed = True
        if changed:
            copied.append(name)
    return copied


@dataclass(frozen=True)
class TimerConfigurationUnreadable:
    reason: str


@dataclass(frozen=True)
class TimerTargetUnresolved:
    target: str


@dataclass(frozen=True)
class TimerScratchAbsent:
    session_id: str


@dataclass(frozen=True)
class TimerScratchFound:
    path: Path


def timer_scratch(staff_id: str, record: staff.StaffSnapshot | None = None
                  ) -> TimerConfigurationUnreadable | TimerTargetUnresolved | TimerScratchAbsent | TimerScratchFound:
    """Find the scratchpad for the session that receives the production update timer."""
    record = record if record is not None else staff.snapshot()
    try:
        target = _fields(NOTIFIER_STATE_DIR / f"showrunner-{staff_id}" / "conf").get("TARGET", "")
    except (OSError, UnicodeError) as error:
        return TimerConfigurationUnreadable(" ".join(str(error).splitlines()))
    owner = record.by_id(staff.StaffId(target.removeprefix("session:")))
    session_id = owner.current_session if owner is not None else ""
    if not session_id or session_id == target:
        return TimerTargetUnresolved(target)
    return next((TimerScratchFound(path) for path in sorted(SCRATCH_ROOT.glob(f"claude-*/*/{session_id}/scratchpad"))
                 if path.is_dir()), TimerScratchAbsent(session_id))


def adopt_session_state(staff_id: str, record: staff.StaffSnapshot | None = None) -> list[str]:
    """Copy missing scratchpad state to the fixed owner's folder."""
    target = session_state_dir(staff_id)
    if all((target / name).exists() for name in SESSION_STATE):
        return []
    scratch = timer_scratch(staff_id, record)
    if isinstance(scratch, TimerConfigurationUnreadable):
        print(f"showrunners: timer configuration unreadable: {scratch.reason}", file=sys.stderr)
    return copy_session_state(staff_id, scratch.path) if isinstance(scratch, TimerScratchFound) else []


def current_name(production_name: str) -> str:
    """The name now of the production's showrunner. Empty while it is not running or has no update timer."""
    return _name_in(production_name, require_complete(registered_showrunners()))


def require_complete(runners: list[Showrunner]) -> list[Showrunner]:
    """Require a complete inventory before an absence or safety decision."""
    if isinstance(runners, ShowrunnerDiscovery) and runners.lookup_failures:
        raise OSError("; ".join(failure.reason for failure in runners.lookup_failures))
    return runners


def _name_in(production_name: str, runners: list[Showrunner]) -> str:
    return next((runner["session"] for runner in runners if runner["name"] == production_name), "")


def current_address(production_name: str, *, deadline: float = math.inf) -> str:
    """Where `SendMessage` reaches the production's showrunner now: its session on this machine.
    Empty while it is not running or has no update timer."""
    return _address_in(production_name, require_complete(registered_showrunners(deadline=deadline)))


def _address_in(production_name: str, runners: list[Showrunner]) -> str:
    socket = next((runner["socket"] for runner in runners if runner["name"] == production_name), "")
    return f"uds:{socket}" if socket else ""


def named(showrunner: str) -> Showrunner:
    """Find the registered showrunner by its staff name."""
    runners = require_complete(registered_showrunners())
    found = next((runner for runner in runners if runner["name"] == showrunner), None)
    if showrunner and found:
        return found
    raise ValueError(f"no showrunner with an update timer is called {showrunner} or runs that production")


def timer_docs(state_dir: Path | None = None) -> dict[str, Path]:
    """Map each production name to its document when its update timer is complete."""
    found: dict[str, Path] = {}
    for instance in (state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*"):
        try:
            timer = _source(instance).timer
        except (OSError, KeyError, ValueError, UnicodeError):
            continue
        found[timer.name] = timer.doc
    return found


def timer_names(state_dir: Path | None = None) -> set[str]:
    """Every production with a complete update timer, whether or not its showrunner runs."""
    return set(timer_docs(state_dir))


def own_name() -> str:
    """The production this session runs as showrunner. Empty when it runs none."""
    row = staff.snapshot().by_current_session(staff.runtime_id())
    return row.name if row is not None and row.kind == "showrunner" else ""


def name_refusal(production_name: str, name: str) -> str:
    """Return the reason when the production name differs from its showrunner's session name."""
    if name == production_name:
        return ""
    if not name:
        return f"the showrunner of {production_name} has no session name; name the session {production_name}"
    return (f"production {production_name} has showrunner {name}, and a production's name must be its showrunner's name; "
            + f"rename the production first: `/agents rename {production_name} {name}`")


@dataclass(frozen=True)
class NoticeDelivery:
    """Output lines and delivery failures from one completed notice batch."""

    lines: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def send_to_address(address: str, key: str, text: str, *, recipient: str,
                    sender: str = "showrunners") -> NoticeDelivery:
    """Send to a known session address with the same report as production notices."""
    sent = subprocess.run(session_send.send_argv("--to", address, "--from", sender, "--key", key,
                           "--text", text, send_script=SEND), capture_output=True, text=True, check=False)
    if sent.returncode != 0:
        return NoticeDelivery((f"could not tell {recipient}: {sent.stderr.strip() or f'send.py exited {sent.returncode}'}",),
                              failed=(recipient,))
    return NoticeDelivery((f"told {recipient}",))


def deliver(production_name: str, key: str, text: str, *, sender: str = "showrunners") -> NoticeDelivery:
    """Resolve a production's current address and report whether its notice failed."""
    address = current_address(production_name)
    if not address:
        return NoticeDelivery((f"{production_name} is not running: tell it when it starts",))
    return send_to_address(address, key, text, recipient=production_name, sender=sender)


def tell(production_name: str, key: str, text: str, *, sender: str = "showrunners") -> str:
    """Send one line and preserve the existing human-readable delivery report."""
    return deliver(production_name, key, text, sender=sender).lines[0]


def tell_others(texts: dict[str, str], key: str) -> NoticeDelivery:
    """Tell each production its line, but for the one this session runs, which reads the command's own output."""
    own = own_name()
    deliveries = [deliver(production_name, key, text) for production_name, text in texts.items() if production_name != own]
    return NoticeDelivery(tuple(line for delivery in deliveries for line in delivery.lines),
                          failed=tuple(production_name for delivery in deliveries for production_name in delivery.failed))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    # Does nothing: a unit's run state is read from its run records. Delete once no session started before that is left.
    making_ready = commands.add_parser("ready")
    _ = making_ready.add_argument("session", nargs="?")
    _ = making_ready.add_argument("--unit", action="append")
    naming = commands.add_parser("name")
    _ = naming.add_argument("production_name")
    addressing = commands.add_parser("address")
    _ = addressing.add_argument("production_name")
    # Does nothing: a unit's run state is read from its run records. Delete once no session started before that is left.
    setting_status = commands.add_parser("status")
    _ = setting_status.add_argument("session", nargs="?")
    _ = setting_status.add_argument("--unit")
    _ = setting_status.add_argument("--state")
    _ = commands.add_parser("list")
    doc_command = commands.add_parser("doc")
    _ = doc_command.add_argument("id")
    for lookup in ("parent", "children", "root"):
        looking_up = commands.add_parser(lookup)
        _ = looking_up.add_argument("production_name")
    _ = commands.add_parser("tree")
    attaching = commands.add_parser("attach")
    _ = attaching.add_argument("child")
    _ = attaching.add_argument("parent", nargs="?")
    detaching = commands.add_parser("detach")
    _ = detaching.add_argument("child")
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    try:
        if action == "doc":
            row = owner_for(cast(str, args.id))
            if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and row.doc.machine == remote.local_machine():
                print(row.doc.path)
                return 0
            return 1
        if action in ("list", "name", "address", "tree"):
            runners = registered_showrunners()
            if action == "list":
                for runner in runners:
                    print(f"{runner['name']}\t{runner['session'] or '<not running>'}\t{runner['zone']}"
                          + f"\t{runner['doc']}")
            elif action == "tree":
                lines = cast(Callable[[list[Showrunner]], list[str]], import_module('scripts.production.showrunner_tree').tree_lines)(runners)
                for line in lines:
                    print(line)
            else:
                production_name = cast(str, args.production_name)
                found = _name_in(production_name, runners) if action == "name" else _address_in(production_name, runners)
                if not found and any(failure.production == production_name for failure in runners.lookup_failures):
                    return runners.exit_code
                if not found:
                    raise ValueError(f"the showrunner of {production_name} is not running")
                print(found)
            return runners.exit_code
        elif action in ("parent", "children", "root"):
            return cast(Callable[[str, str], int], import_module('scripts.production.showrunner_tree').main)(
                action, cast(str, args.production_name))
        elif action in ("attach", "detach"):
            return cast(Callable[[str, str, str], int], import_module('scripts.production.showrunner_parent').main)(
                action, cast(str, args.child), (cast(str | None, args.parent) or "") if action == "attach" else "")
    except (OSError, ValueError, KeyError) as error:
        print(f"showrunners: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
