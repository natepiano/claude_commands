#!/usr/bin/env python3
"""Read local showrunner timers, resolve their addresses and send production notices."""
from __future__ import annotations

import argparse
import json
import math
from time import monotonic
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import NamedTuple, TypedDict, cast, override

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

CONFIG = Path(os.environ.get("SHOWRUNNERS_CONFIG") or Path(__file__).resolve().parents[2] / "config/showrunners.json")
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
SESSIONS_DIR = Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
SESSIONS = Path(os.environ.get("SHOWRUNNERS_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")
SHOWRUNNER_STATE_DIR = Path(os.environ.get("SHOWRUNNER_STATE_DIR") or Path.home() / ".local/state/showrunner")
# Where Claude Code keeps each session scratchpad, as /tmp/claude-<uid>/<project>/<session id>/scratchpad.
SCRATCH_ROOT = Path(os.environ.get("SHOWRUNNER_SCRATCH_ROOT") or "/tmp")
SEND = Path(os.environ.get("SHOWRUNNERS_SEND") or Path(__file__).resolve().parent.parent / "message/send.py")
PARENT_FILE = "parent"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from user_zone import user_zone_name  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
from sessions import SessionRecord, messaging_socket_exists  # noqa: E402
from scripts.production import staff  # noqa: E402

RULES = "Read the Nested showrunners rules in ~/.claude/commands/showrunner/produce.md."


class Showrunner(TypedDict):
    """One showrunner, read from its update timer each time. Nothing here is stored in the config."""

    # The session's name now, looked up by its Claude session id. Empty while it is not running.
    session: str
    # Where a message reaches it now. Empty while it is not running.
    socket: str
    # The production's slug, which names the showrunner whatever its session is called.
    slug: str
    # The user's zone setting. A production doc's own User zone line is no longer read.
    zone: str
    # The production doc, whose Units table says which units the showrunner has.
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


class Timer(NamedTuple):
    """One complete update timer. It is what makes a production's showrunner exist, running or not."""

    slug: str
    target: str
    doc: Path
    zone: str


@dataclass(frozen=True)
class ShowrunnerSource:
    """One readable timer and the production document read with it."""

    timer: Timer
    document: str


class CheckedDoc(NamedTuple):
    path: Path


class NoCheckedDoc(NamedTuple):
    reason: str


def defaults() -> ShowrunnerSettings:
    return ShowrunnerSettings(threshold_percent=2, repeat_minutes=30, stall_minutes=5,
                              faults_to="natedev", always=["natedev"])


def load_settings() -> ShowrunnerSettings:
    return load_settings_from(CONFIG)


def load_settings_from(path: Path) -> ShowrunnerSettings:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise ValueError("expected an object")
        data = cast(dict[str, object], raw)
        for key in ("threshold_percent", "repeat_minutes", "stall_minutes"):
            if not isinstance(data.get(key), (int, float)) or isinstance(data[key], bool):
                raise ValueError(f"invalid {key}")
        if not isinstance(data.get("faults_to"), str) or not data["faults_to"]:
            raise ValueError("invalid faults_to")
        always = data.get("always")
        if not isinstance(always, list) or not all(isinstance(item, str) for item in cast(list[object], always)):
            raise ValueError("invalid always")
        # A file written while showrunners were listed here still has the list; it is not read.
        return ShowrunnerSettings(threshold_percent=cast(float, data["threshold_percent"]),
                                  repeat_minutes=cast(float, data["repeat_minutes"]),
                                  stall_minutes=cast(float, data["stall_minutes"]),
                                  faults_to=cast(str, data["faults_to"]), always=cast(list[str], always))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError(f"{path}: {error}") from error


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


def checked_doc(instance: Path) -> CheckedDoc | NoCheckedDoc:
    """Read the production doc passed to an update instance's production check."""
    try:
        return _checked_doc(_fields(instance / "conf").get("CHECK"))
    except (OSError, UnicodeError) as error:
        return NoCheckedDoc(f"conf could not be read: {error}")


def _checked_doc(check: str | None) -> CheckedDoc | NoCheckedDoc:
    if check is None:
        return NoCheckedDoc("CHECK is missing")
    try:
        command = shlex.split(check)
    except ValueError as error:
        return NoCheckedDoc(f"CHECK could not be parsed: {error}")
    try:
        script_index = next(index for index, token in enumerate(command)
                            if Path(token).name == "production_check.sh")
    except StopIteration:
        return NoCheckedDoc("CHECK has no production_check.sh token")
    if script_index + 1 >= len(command):
        return NoCheckedDoc("CHECK has no production doc token")
    path = Path(command[script_index + 1])
    if not path.is_absolute():
        return NoCheckedDoc("production doc path is relative")
    return CheckedDoc(path)


def session_name(session_id: str) -> str:
    """The live Claude session's name, or empty when it has none or is not running."""
    return _session_name(session_id)


def _session_name(session_id: str) -> str:
    for path in SESSIONS_DIR.glob("*.json"):
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
            expected_document: Path | None = None) -> ShowrunnerSource:
    fields = _fields(instance / "conf", dependencies=dependencies)
    target = fields["TARGET"]
    if not target.startswith("session:") or not target[8:]:
        raise ValueError("invalid TARGET")
    doc = _checked_doc(fields.get("CHECK"))
    if isinstance(doc, NoCheckedDoc):
        raise ValueError(doc.reason)
    if expected_document is not None and doc.path.resolve() != expected_document.resolve():
        raise ValueError(f"{instance}: timer document differs from stored document {expected_document}")
    if dependencies is not None:
        dependencies(doc.path)
    try:
        document = doc.path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"production doc could not be read: {error}") from error
    timer = Timer(instance.name.removeprefix("showrunner-"), target, doc.path, user_zone_name())
    return ShowrunnerSource(timer, document)


def read_bound_source(instance: Path, document: Path) -> ShowrunnerSource:
    """Read a timer only when its document agrees with the caller's stored owner."""
    return _source(instance, expected_document=document)


def _timer(instance: Path) -> Timer:
    return _source(instance).timer


class UnreadableTimerPolicy(Enum):
    """Whether unreadable timer evidence must stop discovery or may be skipped by seed."""

    RAISE = "raise"
    SKIP = "skip"
    STRICT = "strict"  # Audit every source, including incomplete timer declarations.


def read_sources(state_dir: Path | None = None, *,
                 unreadable: UnreadableTimerPolicy,
                 dependencies: Callable[[Path], None] | None = None) -> list[ShowrunnerSource]:
    """Read each timer/doc once, applying the caller's policy to unreadable timers."""
    found: list[ShowrunnerSource] = []
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        if not instance.is_dir():
            continue
        try:
            found.append(_source(instance, dependencies=dependencies))
        except (OSError, KeyError, ValueError, UnicodeError) as error:
            if unreadable is UnreadableTimerPolicy.STRICT:
                raise ValueError(f"{instance / 'conf'}: {error}") from error
            if isinstance(error, OSError) and unreadable is UnreadableTimerPolicy.RAISE:
                raise
            print(f"showrunners: skipping {instance.name}: {error}", file=sys.stderr)
    return found


class DiscoverSessions(NamedTuple):
    """No registry snapshot was supplied; resolve sessions from their current records."""


DISCOVER_SESSIONS = DiscoverSessions()


class DiscoverMembership(NamedTuple):
    """Read staff when no report snapshot was supplied."""


DISCOVER_MEMBERSHIP = DiscoverMembership()


def _registered(timer: Timer, *,
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
    return Showrunner(session=name, socket=socket or "", slug=timer.slug, zone=timer.zone, doc=str(timer.doc))


def registered_showrunners(
    state_dir: Path | None = None, *,
    known_sessions: Mapping[str, SessionRecord] | DiscoverSessions = DISCOVER_SESSIONS,
    deadline: float = math.inf,
    membership: staff.StaffSnapshot | DiscoverMembership = DISCOVER_MEMBERSHIP,
) -> ShowrunnerDiscovery:
    """Every showrunner that has an update timer, running or not.

    The timer is addressed to the Claude session id and holds the production doc, so it is the one
    record of a showrunner and holds no name: the name is looked up here, at each call. Raises OSError
    when a timer conf cannot be read. Incomplete registrations are left out with a warning as before.
    A failed session lookup is left out with the reason on stderr and in the result's failures.
    Commands must return its exit_code
    after processing the healthy rows. Supplied live session records avoid
    rereading the registry or starting the sessions subprocess; an empty mapping means none are live.
    """
    found: list[Showrunner] = []
    failures: list[ShowrunnerLookupFailure] = []
    sources = read_sources(state_dir, unreadable=UnreadableTimerPolicy.RAISE)
    membership = staff.initial_snapshot() if isinstance(membership, DiscoverMembership) else membership
    for index, source in enumerate(sources):
        try:
            # Share the remaining budget with later lookups and the caller's inspection work.
            began = monotonic()
            lookup_deadline = began + max(0.0, deadline - began) / (len(sources) - index + 1)
            found.append(_registered(source.timer, known_sessions=known_sessions, deadline=lookup_deadline, membership=membership))
        except (OSError, KeyError, ValueError, UnicodeError) as error:
            failure = ShowrunnerLookupFailure(source.timer.slug, str(error))
            if isinstance(error, OSError):
                failures.append(failure)
            print(failure.diagnostic, file=sys.stderr)
    return ShowrunnerDiscovery(found, tuple(failures))


def production_of(session_id: str, state_dir: Path | None = None) -> Path | None:
    """The production doc of the showrunner whose update timer is addressed to `session_id`."""
    owner = staff.initial_snapshot().by_current_session(session_id) if session_id else None
    reference = owner.id if owner is not None else session_id
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        try:
            target = _fields(instance / "conf").get("TARGET")
        except (OSError, UnicodeError):
            continue
        doc = checked_doc(instance)
        if session_id and target == f"session:{reference}" and isinstance(doc, CheckedDoc):
            return doc.path
    return None


def production_slug(doc: str) -> str:
    """The slug a production's units are marked with: its doc's name without the suffix."""
    slug = Path(doc).name.removesuffix("-production.md")
    if not doc or slug == Path(doc).name:
        raise ValueError(f"not a production doc: {doc or 'none registered'}")
    return slug


def session_state_dir(slug: str) -> Path:
    """The showrunner's own state, which a new session id, a restart and a reboot keep; a wrap deletes it."""
    return SHOWRUNNER_STATE_DIR / slug / "session"


# The showrunner state a scratchpad held before session_state_dir.
SESSION_STATE = ("dailies_input_state", "unit_status", "showrunner_state.json", "dailies_judgment.json",
                 "dailies_state.json")


def copy_session_state(slug: str, scratch: Path) -> list[str]:
    """Copy each state file the scratchpad holds and the state directory does not; a copy never replaces one."""
    target = session_state_dir(slug)
    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in SESSION_STATE:
        source, destination = scratch / name, target / name
        if destination.exists() or not source.exists():
            continue
        complete = target / f".{name}.copying"
        if complete.is_dir():
            shutil.rmtree(complete)
        else:
            complete.unlink(missing_ok=True)
        if source.is_dir():
            _ = shutil.copytree(source, complete)
        else:
            _ = shutil.copy2(source, complete)
        os.replace(complete, destination)
        copied.append(name)
    return copied


def timer_scratch(slug: str) -> Path | None:
    """The scratchpad of the session the production's update timer is addressed to, when it exists."""
    try:
        target = _fields(NOTIFIER_STATE_DIR / f"showrunner-{slug}" / "conf").get("TARGET", "")
    except (OSError, UnicodeError):
        return None
    session_id = target.removeprefix("session:")
    if not session_id or session_id == target:
        return None
    return next((path for path in sorted(SCRATCH_ROOT.glob(f"claude-*/*/{session_id}/scratchpad"))
                 if path.is_dir()), None)


def adopt_session_state(slug: str) -> list[str]:
    """Bring a showrunner's scratchpad state into its state directory once, with no step by anyone.

    Only the state files the directory lacks are looked for, so a showrunner that has moved costs one
    stat per file.
    """
    target = session_state_dir(slug)
    if all((target / name).exists() for name in SESSION_STATE):
        return []
    scratch = timer_scratch(slug)
    return copy_session_state(slug, scratch) if scratch is not None else []


def former_slugs(text: str) -> tuple[str, ...]:
    """Read former names only from the production's Production Context section."""
    context = re.search(r"^## Production Context\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    formerly = re.search(r"^- \*\*Formerly:\*\*(.*)$", context[1] if context else "", re.MULTILINE)
    return tuple(re.findall(r"`([^`]+)`", formerly[1])) if formerly else ()


def canonical_slug(slug: str, state_dir: Path | None = None) -> str:
    """Map a former production name to its current slug; leave other slugs unchanged."""
    timers: list[Timer] = []
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        try:
            timer = _timer(instance)
        except (OSError, KeyError, ValueError, UnicodeError):
            continue
        if timer.slug == slug:
            return slug
        timers.append(timer)
    for timer in timers:
        try:
            text = timer.doc.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if slug in former_slugs(text):
            return timer.slug
    return slug


def current_name(slug: str) -> str:
    """The name now of the production's showrunner. Empty while it is not running or has no update timer."""
    return _name_in(slug, require_complete(registered_showrunners()))


def require_complete(runners: list[Showrunner]) -> list[Showrunner]:
    """Require a complete inventory before an absence or safety decision."""
    if isinstance(runners, ShowrunnerDiscovery) and runners.lookup_failures:
        raise OSError("; ".join(failure.reason for failure in runners.lookup_failures))
    return runners


def _name_in(slug: str, runners: list[Showrunner]) -> str:
    slug = canonical_slug(slug)
    return next((runner["session"] for runner in runners if runner["slug"] == slug), "")


def herdr_space(slug: str, doc: str) -> str:
    """The shared herdr session's workspace for a production, named for its showrunner.

    The doc's `Showrunner session:` line names it, else the slug. The name never depends on whether
    the showrunner runs, so a unit restarted before its showrunner finds the same workspace.
    """
    try:
        text = Path(doc).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        text = ""
    return space_from_text(slug, text)


def space_from_text(slug: str, text: str) -> str:
    """The workspace name from an already-read production document."""
    line = re.search(r"^- \*\*Showrunner session:\*\*\s*`?([^`\s]+)`?\s*$", text, re.MULTILINE)
    return line[1] if line else slug


def current_address(slug: str, *, deadline: float = math.inf) -> str:
    """Where `SendMessage` reaches the production's showrunner now: its session on this machine.
    Empty while it is not running or has no update timer."""
    return _address_in(slug, require_complete(registered_showrunners(deadline=deadline)))


def _address_in(slug: str, runners: list[Showrunner]) -> str:
    slug = canonical_slug(slug)
    socket = next((runner["socket"] for runner in runners if runner["slug"] == slug), "")
    return f"uds:{socket}" if socket else ""


def named(showrunner: str) -> Showrunner:
    """The showrunner called `showrunner` now, or whose production has that slug."""
    slug = canonical_slug(showrunner)
    runners = require_complete(registered_showrunners())
    # The production a slug resolves to wins over a session that happens to share the name.
    found = (next((runner for runner in runners if runner["slug"] == slug), None)
             or next((runner for runner in runners if runner["session"] == showrunner), None))
    if showrunner and found:
        return found
    raise ValueError(f"no showrunner with an update timer is called {showrunner} or runs that production")


def timer_docs(state_dir: Path | None = None) -> dict[str, Path]:
    """Each production with a complete update timer, by slug, with its doc, whether or not its showrunner runs."""
    found: dict[str, Path] = {}
    for instance in (state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*"):
        try:
            timer = _timer(instance)
        except (OSError, KeyError, ValueError, UnicodeError):
            continue
        found[timer.slug] = timer.doc
    return found


def timer_slugs(state_dir: Path | None = None) -> set[str]:
    """Every production with a complete update timer, whether or not its showrunner runs."""
    return set(timer_docs(state_dir))


def own_slug() -> str:
    """The production this session runs as showrunner. Empty when it runs none."""
    doc = production_of(staff.runtime_id())
    return production_slug(str(doc)) if doc is not None else ""


def name_refusal(slug: str, name: str) -> str:
    """Why a showrunner session called `name` cannot run production `slug`; empty when it can.

    One name per production (user, 2026-10-27): the slug is the showrunner's name, so a production
    whose showrunner has another name is renamed first.
    """
    if name == slug:
        return ""
    if not name:
        return f"the showrunner of {slug} has no session name; name the session {slug}"
    return (f"production {slug} has showrunner {name}, and a production's slug must be its showrunner's name; "
            + f"rename the production first: `/showrunner:rename plan {slug} {name}`")


@dataclass(frozen=True)
class NoticeDelivery:
    """Output lines and delivery failures from one completed notice batch."""

    lines: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def send_to_address(address: str, key: str, text: str, *, recipient: str,
                    sender: str = "showrunners") -> NoticeDelivery:
    """Send to a known session address with the same report as production notices."""
    sent = subprocess.run([sys.executable, str(SEND), "--to", address, "--from", sender, "--key", key,
                           "--text", text], capture_output=True, text=True, check=False)
    if sent.returncode != 0:
        return NoticeDelivery((f"could not tell {recipient}: {sent.stderr.strip() or f'send.py exited {sent.returncode}'}",),
                              failed=(recipient,))
    return NoticeDelivery((f"told {recipient}",))


def deliver(slug: str, key: str, text: str, *, sender: str = "showrunners") -> NoticeDelivery:
    """Resolve a production's current address and report whether its notice failed."""
    address = current_address(slug)
    if not address:
        return NoticeDelivery((f"{slug} is not running: tell it when it starts",))
    return send_to_address(address, key, text, recipient=slug, sender=sender)


def tell(slug: str, key: str, text: str, *, sender: str = "showrunners") -> str:
    """Send one line and preserve the existing human-readable delivery report."""
    return deliver(slug, key, text, sender=sender).lines[0]


def tell_others(texts: dict[str, str], key: str) -> NoticeDelivery:
    """Tell each production its line, but for the one this session runs, which reads the command's own output."""
    own = own_slug()
    deliveries = [deliver(slug, key, text) for slug, text in texts.items() if slug != own]
    return NoticeDelivery(tuple(line for delivery in deliveries for line in delivery.lines),
                          failed=tuple(slug for delivery in deliveries for slug in delivery.failed))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    # Does nothing: a unit's run state is read from its run records. Delete once no session started before that is left.
    making_ready = commands.add_parser("ready")
    _ = making_ready.add_argument("session", nargs="?")
    _ = making_ready.add_argument("--unit", action="append")
    naming = commands.add_parser("name")
    _ = naming.add_argument("slug")
    addressing = commands.add_parser("address")
    _ = addressing.add_argument("slug")
    # Does nothing: a unit's run state is read from its run records. Delete once no session started before that is left.
    setting_status = commands.add_parser("status")
    _ = setting_status.add_argument("session", nargs="?")
    _ = setting_status.add_argument("--unit")
    _ = setting_status.add_argument("--state")
    _ = commands.add_parser("list")
    for lookup in ("parent", "children", "root"):
        looking_up = commands.add_parser(lookup)
        _ = looking_up.add_argument("slug")
    _ = commands.add_parser("tree")
    attaching = commands.add_parser("attach")
    _ = attaching.add_argument("child")
    _ = attaching.add_argument("parent", nargs="?")
    detaching = commands.add_parser("detach")
    _ = detaching.add_argument("child")
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    try:
        if action in ("list", "name", "address", "tree"):
            runners = registered_showrunners()
            if action == "list":
                for runner in runners:
                    print(f"{runner['slug']}\t{runner['session'] or '<not running>'}\t{runner['zone']}"
                          + f"\t{runner['doc']}")
            elif action == "tree":
                lines = cast(Callable[[list[Showrunner]], list[str]], import_module("showrunner_tree").tree_lines)(runners)
                for line in lines:
                    print(line)
            else:
                slug = cast(str, args.slug)
                found = _name_in(slug, runners) if action == "name" else _address_in(slug, runners)
                if not found and any(failure.production == canonical_slug(slug) for failure in runners.lookup_failures):
                    return runners.exit_code
                if not found:
                    raise ValueError(f"the showrunner of {slug} is not running")
                print(found)
            return runners.exit_code
        elif action in ("parent", "children", "root"):
            return cast(Callable[[str, str], int], import_module("showrunner_tree").main)(
                action, cast(str, args.slug))
        elif action in ("attach", "detach"):
            return cast(Callable[[str, str, str], int], import_module("showrunner_parent").main)(
                action, cast(str, args.child), (cast(str | None, args.parent) or "") if action == "attach" else "")
    except (OSError, ValueError, KeyError) as error:
        print(f"showrunners: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
