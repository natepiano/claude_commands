#!/usr/bin/env python3
"""Inventory the live Claude work belonging to one account on this machine."""

from __future__ import annotations

import json
import os
import re
import shutil
import shlex
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast, override

SCRIPTS = Path(__file__).resolve().parents[1]
# Sibling imports need this folder. Append it so a caller's own modules
# (buildlog's record) keep precedence over shutdown's.
if str(SCRIPTS / "shutdown") not in sys.path:
    sys.path.append(str(SCRIPTS / "shutdown"))
for dependency in ("whoami", "message", "production", "herdr", "direct", "lib", "hooks"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import account
import conversation_pause
from probe_session import PROBE_ENV
import unit_runs
if TYPE_CHECKING:
    from shutdown_record import PausedSeat
    from ..direct.worker_state import worker_ledger
else:
    from worker_state import worker_ledger
import detection_rules
import herdr
import sessions
from sessions import process_start as _process_start
import showrunners
import unit_lookup
from account import Account, label_for


from session_types import (
    KnownBoot as KnownBoot,
    BootNotRecorded as BootNotRecorded,
    CompleteInventory as CompleteInventory,
    PartialInventory as PartialInventory,
    InvalidInventory as InvalidInventory,
    parse_session as parse_session,
    wire_object as _wire_object,
    wire_required as _wire_required,
    wire_string as _wire_string,
    wire_integer as _wire_integer,
    wire_list as _wire_list,

    PlanFromRunRecord as PlanFromRunRecord,
    NoRunRecord as NoRunRecord,
    UnitPlan as UnitPlan,
    UnitHost as UnitHost,
    LegacyTmuxUnitHost as LegacyTmuxUnitHost,
    LegacyTmuxHost as LegacyTmuxHost,
    HerdrHost as HerdrHost,
    NamedDesktop as NamedDesktop,
    DesktopNotInSnapshot as DesktopNotInSnapshot,
    Desktop as Desktop,
    SupervisorIdentity as SupervisorIdentity,
    NoSupervisor as NoSupervisor,
    WindowHost as WindowHost,
    TerminalHost as TerminalHost,
    UnknownHost as UnknownHost,
    Host as Host,
    SessionHost as SessionHost,
    OnBranch as OnBranch,
    DetachedHead as DetachedHead,
    Head as Head,
    Tracking as Tracking,
    NoUpstream as NoUpstream,
    Upstream as Upstream,
    GitCheckout as GitCheckout,
    NotACheckout as NotACheckout,
    CheckoutState as CheckoutState,
    ModelName as ModelName,
    NoReplyYet as NoReplyYet,
    LastModel as LastModel,
    DirectorOwner as DirectorOwner,
    NoDirector as NoDirector,
    WorkerOwner as WorkerOwner,
    CodexServer as CodexServer,
    SessionFields as SessionFields,
    ShowrunnerSession as ShowrunnerSession,
    UnitSession as UnitSession,
    WorkerSession as WorkerSession,
    TopLevelSession as TopLevelSession,
    Session as Session,
    TimerRestore as TimerRestore,
    CodexSessionId as CodexSessionId,
    CodexResumePicker as CodexResumePicker,
    CodexLaunch as CodexLaunch,
    SnapshotClaudeSession as SnapshotClaudeSession,
    SnapshotCodexSession as SnapshotCodexSession,
    SnapshotSession as SnapshotSession,
    RecoveryHostProcess as RecoveryHostProcess,
    RecoveryHostUnrecorded as RecoveryHostUnrecorded,
)


def herdr_presentation(host: HerdrHost) -> Literal["attached window", "detached"]:
    """Records made before window presentation was saved remain detached."""
    return host.get("presentation", "detached")


def supervisor_identity(host: WindowHost | TerminalHost) -> SupervisorIdentity | NoSupervisor:
    """Older inventories and records predate the window supervisor."""
    return host.get("supervisor", NoSupervisor(kind="no supervisor"))


def live_supervisor(host: WindowHost | TerminalHost) -> SupervisorIdentity | NoSupervisor:
    identity = supervisor_identity(host)
    if (identity["kind"] == "supervisor" and identity["proc_start"]
            and _process_start(identity["pid"]) == identity["proc_start"]):
        return identity
    return NoSupervisor(kind="no supervisor")



@dataclass(frozen=True)
class ListedSession:
    """A live, attributed session without its worktree or restart resources."""

    session_id: str
    pid: int
    proc_start: str
    name: str
    kind: Literal["showrunner", "unit", "worker", "top-level"]
    config_dir: Path
    login: str
    # Empty for sessions outside a registered production.
    production: str
    doc: str


class UnattributedSession(TypedDict):
    pid: int
    name: str
    reason: Literal["account unreadable", "process start mismatch"]


class Inventory(TypedDict):
    """Everything this phase can safely attribute to one account on one machine."""

    machine: str
    login: str
    label: str
    sessions: list[Session]
    unattributed: list[UnattributedSession]


class SessionFile(TypedDict, total=False):
    """Fields used from the richer on-disk session record."""

    pid: int
    sessionId: str
    cwd: str
    kind: str
    name: str
    status: str
    tmux: str
    messagingSocketPath: str
    updatedAt: int
    formerNames: list[str]
    procStart: str


@dataclass(frozen=True)
class ProductionSession:
    slug: str
    doc: str


@dataclass(frozen=True)
class ShowrunnerRegistration:
    session_id: str
    slug: str
    doc: str


@dataclass(frozen=True)
class ShowrunnerRegistrations:
    entries: tuple[ShowrunnerRegistration, ...]


@dataclass(frozen=True)
class UnitMarks:
    production: str
    unit: str
    doc: str
    session_name: str
    session_host: Literal["herdr"]


DESKTOP_HEADING = re.compile(r"^##\s+desktop:\s*(.+?)\s*$")
SNAPSHOT_SESSION = re.compile(r"^`(.+)` · (\S+) · (.+?)\s*$")
SNAPSHOT_COMMIT_PREFIX = "agent-sessions-snapshot: "
RESUME_ID = re.compile(r"(?:^|\s)--resume\s+['\"]?([^\s'\"]+)")


def parse_inventory(text: str) -> Inventory:
    """Decode and recursively validate one JSON inventory."""
    try:
        value = cast(object, json.loads(text))
    except (ValueError, TypeError):
        raise InvalidInventory("inventory is not valid JSON") from None
    data = _wire_object(value, "inventory")
    parsed_sessions = [
        parse_session(item, f"inventory.sessions[{index}]", require_updated_at=True)
        for index, item in enumerate(
            _wire_list(
                _wire_required(data, "sessions", "inventory"),
                "inventory.sessions",
            )
        )
    ]
    unattributed: list[UnattributedSession] = []
    for index, item in enumerate(
        _wire_list(
            _wire_required(data, "unattributed", "inventory"),
            "inventory.unattributed",
        )
    ):
        place = f"inventory.unattributed[{index}]"
        report = _wire_object(item, place)
        reason = _wire_string(
            _wire_required(report, "reason", place), f"{place}.reason"
        )
        if reason not in {"account unreadable", "process start mismatch"}:
            raise InvalidInventory(f"{place}.reason is invalid")
        unattributed.append(
            cast(
                UnattributedSession,
                cast(
                    object,
                    {
                        "pid": _wire_integer(
                            _wire_required(report, "pid", place), f"{place}.pid"
                        ),
                        "name": _wire_string(
                            _wire_required(report, "name", place),
                            f"{place}.name",
                        ),
                        "reason": reason,
                    },
                ),
            )
        )
    return {
        "machine": _wire_string(
            _wire_required(data, "machine", "inventory"), "inventory.machine"
        ),
        "login": _wire_string(
            _wire_required(data, "login", "inventory"), "inventory.login"
        ),
        "label": _wire_string(
            _wire_required(data, "label", "inventory"), "inventory.label"
        ),
        "sessions": parsed_sessions,
        "unattributed": unattributed,
    }


def _json_object(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _string(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _notifier_state_dir() -> Path:
    return Path(os.environ.get("NOTIFIER_STATE_DIR", str(Path.home() / ".local/state/notifier")))


def _run_roots() -> tuple[Path, ...]:
    return unit_runs.run_roots(os.environ.get("SHUTDOWN_RUN_ROOT"))


def _session_file(record: sessions.SessionRecord) -> SessionFile:
    """Add fields that the shared routing reader deliberately does not retain."""
    data = cast(dict[str, object], cast(object, record)).copy()
    pid = record["pid"]
    path = sessions.sessions_dir() / f"{pid}.json"
    data.update(_json_object(path))
    return cast(SessionFile, cast(object, data))


@dataclass(frozen=True)
class AttributedSessionConfiguration:
    """A session's readable account and the directory that supplied it."""

    account: Account
    config_dir: Path


@dataclass(frozen=True)
class UnreadableSessionConfiguration:
    """A session whose process environment or account cannot be attributed."""


def _configuration_for_session(
    pid: int, home_state: account.LegacyHomeLogin | account.ConfigOnlyHome,
) -> AttributedSessionConfiguration | UnreadableSessionConfiguration:
    """Keep account lookup at one boundary so an unreadable process stays unattributed."""
    try:
        return _configuration_from_setting(account.process_config_dir(pid), home_state)
    except (OSError, UnicodeError, ValueError, TypeError):
        return UnreadableSessionConfiguration()


def _configuration_from_setting(
    setting: account.ConfiguredClaudeFolder | account.NoAccountSetting | account.ProcessEnvironmentUnread,
    home_state: account.LegacyHomeLogin | account.ConfigOnlyHome,
) -> AttributedSessionConfiguration | UnreadableSessionConfiguration:
    """Attribute one observed setting against this pass's shared home state."""
    try:
        setting = account.process_account_directory(setting, home_state=home_state)
        if not isinstance(setting, account.ConfiguredClaudeFolder):
            return UnreadableSessionConfiguration()
        config_dir = setting.path
        if config_dir == Path.home() / ".claude":
            attributed = home_state.account if isinstance(home_state, account.LegacyHomeLogin) else None
        else:
            attributed = account.claude_account(config_dir)
        if attributed is None:
            return UnreadableSessionConfiguration()
        return AttributedSessionConfiguration(attributed, config_dir)
    except (OSError, UnicodeError, ValueError, TypeError):
        return UnreadableSessionConfiguration()


def _record_matches_process(record: SessionFile) -> bool:
    recorded = _string(record.get("procStart"))
    current = _process_start(record.get("pid", 0))
    return bool(recorded) and current is not None and recorded == current


def _conf(path: Path) -> dict[str, str]:
    try:
        return dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
    except (OSError, UnicodeError):
        return {}


def showrunner_registrations() -> ShowrunnerRegistrations:
    """Read timer identities and production docs without resolving messaging sockets."""
    found: list[ShowrunnerRegistration] = []
    for instance in sorted(_notifier_state_dir().glob("showrunner-*")):
        if not instance.is_dir():
            continue
        fields = _conf(instance / "conf")
        target = fields.get("TARGET", "")
        if not target.startswith("session:") or not target[8:]:
            continue
        doc = showrunners.checked_doc(instance)
        if isinstance(doc, showrunners.NoCheckedDoc):
            continue
        try:
            _ = doc.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        found.append(ShowrunnerRegistration(target[8:], instance.name.removeprefix("showrunner-"), str(doc.path)))
    return ShowrunnerRegistrations(tuple(found))


def unregistered_showrunners(
    listed: Sequence[ListedSession] | Literal["read sessions"] = "read sessions", *,
    registrations: ShowrunnerRegistrations | Literal["read timers"] = "read timers",
) -> list[showrunners.Showrunner]:
    """Each production whose update timer names a session that is not running.

    A showrunner is known only by the session its timer names, so one running under another id
    without registering reads as a plain session. Empty when the session records cannot be read.
    """
    try:
        timers = showrunner_registrations() if isinstance(registrations, str) else registrations
        entries = listing(registrations=timers) if isinstance(listed, str) else listed
    except OSError:
        return []
    live_ids = {entry.session_id for entry in entries}
    return [showrunners.Showrunner(slug=timer.slug, doc=timer.doc, session="", socket="", zone="")
            for timer in timers.entries if timer.session_id not in live_ids]


def _unit_sessions(runners: tuple[ShowrunnerRegistration, ...]) -> dict[str, UnitMarks]:
    """Each running unit director of a registered production, by session id.

    One read of every production's marks (`unit_lookup.every_marked_unit`) serves them all. A
    production with no showrunner timer has no doc to restart its units from, so its units are not
    counted here; a production whose marks are at fault is left out by the lookup.
    """
    try:
        every = unit_lookup.every_marked_unit()
    except OSError:
        return {}
    docs = {runner.slug: runner.doc for runner in runners}
    found: dict[str, UnitMarks] = {}
    for slug, units in every.units.items():
        if slug not in docs:
            continue
        for unit_id, marked_unit in units.items():
            claude = marked_unit.claude
            if not isinstance(claude, unit_lookup.LiveClaude):
                continue
            found[claude.session_id] = UnitMarks(
                production=slug,
                unit=unit_id,
                doc=docs[slug],
                session_name=marked_unit.place.session,
                session_host="herdr",
            )
    return found


def _active_run_directories() -> dict[str, list[Path]]:
    """Every director-to-run mapping, including directors that have already exited."""
    found: dict[str, list[Path]] = {}
    mappings: list[Path] = []
    for root in _run_roots():
        try:
            mappings.extend(sorted((root / "active").iterdir()))
        except OSError:
            continue
    for mapping in mappings:
        if not mapping.is_file():
            continue
        try:
            lines = mapping.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        if lines and lines[0].strip():
            found.setdefault(mapping.name, []).append(Path(lines[0].strip()).expanduser())
    return found


def _worker_owners(run_directories: dict[str, list[Path]], session_ids: set[str]) -> dict[str, str]:
    short_to_session = {session_id[:8].casefold(): session_id for session_id in session_ids}
    owners: dict[str, str] = {}
    for owner, directories in run_directories.items():
        for directory in directories:
            try:
                lines = worker_ledger(directory).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                daemon_short = line.split("\t", 1)[0].strip().casefold()
                worker = short_to_session.get(daemon_short)
                if worker is not None:
                    owners[worker] = owner
    return owners


def _newest_unit_plans() -> dict[str, str]:
    newest: dict[str, tuple[float, str]] = {}
    directory = unit_lookup.runs_dir()
    try:
        paths = list(directory.glob("*.jsonl"))
    except OSError:
        return {}
    for path in paths:
        try:
            first_line = path.read_text(encoding="utf-8").splitlines()[0]
            value = cast(object, json.loads(first_line))
            modified = path.stat().st_mtime
        except (OSError, UnicodeError, ValueError, TypeError, IndexError):
            continue
        if not isinstance(value, dict):
            continue
        event = cast(dict[str, object], value)
        main_agent = event.get("main_agent")
        if not isinstance(main_agent, dict):
            continue
        session_id = _string(cast(dict[str, object], main_agent).get("session_id"))
        plan = _string(event.get("plan_doc")) or _string(event.get("plan"))
        started = event.get("run_started_at")
        rank = float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else modified
        if session_id and plan and (session_id not in newest or rank > newest[session_id][0]):
            newest[session_id] = (rank, plan)
    return {session_id: plan for session_id, (_, plan) in newest.items()}


def _process_environment(pid: int) -> dict[str, str]:
    if sys.platform == "darwin":
        try:
            return herdr.darwin_process_environment(pid)
        except (herdr.HerdrFailure, subprocess.TimeoutExpired):
            return {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    entries = (os.fsdecode(entry).split("=", 1) for entry in raw.split(b"\0") if b"=" in entry)
    return {name: value for name, value in entries}


def terminal_kind(command: str) -> Literal["ghostty", "zed", "terminal", "unknown"]:
    name = Path(command).name.casefold().removeprefix(".")
    matched = ""
    for terminal in ("zed-editor", "ghostty", "zed", "terminal"):
        if name == terminal or (
            name.startswith(terminal)
            and "-wrapped".startswith(name[len(terminal):])
        ):
            matched = terminal
            break
    if matched in {"ghostty", "terminal"} and sys.platform == "darwin":
        return "terminal"
    if matched == "ghostty":
        return "ghostty"
    if matched in {"zed", "zed-editor"}:
        return "zed"
    if matched == "terminal":
        return "terminal"
    return "unknown"


def _supervisor_process(pid: int) -> SupervisorIdentity | NoSupervisor:
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0")
    except (OSError, UnicodeError):
        try:
            result = subprocess.run(("ps", "-o", "args=", "-p", str(pid)),
                                    capture_output=True, text=True, timeout=5, check=False)
            argv = shlex.split(result.stdout) if result.returncode == 0 else []
        except (OSError, ValueError, subprocess.TimeoutExpired):
            argv = []
    if any(Path(argument).name == "relaunch.py" and argv[index + 1:index + 3] == ["run", "--"]
           for index, argument in enumerate(argv)):
        start = _process_start(pid)
        if start:
            return SupervisorIdentity(kind="supervisor", pid=pid, proc_start=start)
    return NoSupervisor(kind="no supervisor")


def _terminal_host(pid: int, desktop: Desktop) -> SessionHost:
    seen: set[int] = set()
    current = pid
    child = 0
    supervisor: SupervisorIdentity | NoSupervisor = NoSupervisor(kind="no supervisor")
    for _ in range(64):
        if current <= 1 or current in seen:
            break
        seen.add(current)
        try:
            result = subprocess.run(
                ("ps", "-o", "ppid=,comm=", "-p", str(current)),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        fields = result.stdout.strip().split(maxsplit=1)
        if result.returncode or len(fields) != 2:
            break
        try:
            parent = int(fields[0])
        except ValueError:
            break
        if current == pid:
            supervisor = _supervisor_process(parent)
        terminal = terminal_kind(fields[1])
        if terminal != "unknown":
            if terminal in {"ghostty", "zed"} and child > 0:
                window = {
                    "kind": terminal,
                    "window_shell": child,
                    "desktop": desktop,
                    "supervisor": (supervisor if supervisor.get("pid") != child
                                   else NoSupervisor(kind="no supervisor")),
                }
                return cast(WindowHost, cast(object, window))
            if terminal == "terminal":
                if child > 0 and supervisor["kind"] == "supervisor" and supervisor["pid"] != child:
                    return {"kind": "terminal", "supervisor": supervisor}
                return {"kind": "terminal"}
            return {"kind": "unknown"}
        child = current
        current = parent
    return {"kind": "unknown"}


# The updater's main() also refreshes the vault's agent notes, which reads every Claude login.
# Shutdown needs only the desktops; an updater from before the flag ignores it and stays correct.
SKIP_AGENT_NOTES = "AGENT_SESSIONS_SKIP_AGENT_NOTES"


def _snapshot_desktops(*, refresh: bool = True) -> dict[str, str]:
    if sys.platform == "darwin":
        return {}
    snapshotter = (shutil.which("agent-sessions-snapshot")
                   if refresh and os.environ.get("REFUSAL_WATCH_INVENTORY") != "read existing" else None)
    if snapshotter is not None:
        try:
            _ = subprocess.run(
                (snapshotter,),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env={**os.environ, SKIP_AGENT_NOTES: "1"},
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    snapshot = Path.home() / "rust/hanadocs/agent sessions.md"
    try:
        lines = snapshot.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    found: dict[str, str] = {}
    desktop = ""
    in_fence = False
    has_session = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and has_session:
            match = RESUME_ID.search(line)
            if match is not None and desktop:
                found[match.group(1)] = desktop
            has_session = False
            continue
        heading = DESKTOP_HEADING.match(line)
        if heading is not None:
            desktop = heading.group(1)
            continue
        has_session = SNAPSHOT_SESSION.match(line) is not None
    return found


def _host(
    session: SessionFile,
    unit: UnitMarks | None,
    plans: dict[str, str],
    desktops: dict[str, str],
) -> Host:
    session_id = _string(session.get("sessionId"))
    environment = _process_environment(session.get("pid", 0))
    if environment.get("TMUX_PANE"):
        return {"kind": "unknown"}
    if unit is not None:
        plan = plans.get(session_id)
        return {
            "kind": "unit",
            "production": unit.production,
            "unit": unit.unit,
            "doc": unit.doc,
            "session_name": unit.session_name,
            "session_host": unit.session_host,
            "plan": (
                {"kind": "plan", "path": plan}
                if plan is not None
                else {"kind": "no run record"}
            ),
        }

    place = herdr.herdr_place_of(session.get("pid", 0))
    if isinstance(place, herdr.HerdrPlace):
        clients = herdr.attached_clients(place.session)
        return {"kind": "herdr", "herdr_session": place.session, "pane_id": place.pane,
                "presentation": "attached window" if isinstance(clients, herdr.ClientsAttached) else "detached"}
    desktop_name = desktops.get(session_id)
    desktop: Desktop = (
        {"kind": "named", "name": desktop_name}
        if desktop_name is not None
        else {"kind": "not in snapshot"}
    )
    return _terminal_host(session.get("pid", 0), desktop)


def _git(cwd: str, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ("git", "-C", cwd, *arguments),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _checkout(cwd: str) -> CheckoutState:
    if not cwd:
        return {"kind": "not a checkout"}
    root = _git(cwd, "rev-parse", "--show-toplevel")
    branch = _git(cwd, "branch", "--show-current")
    head = _git(cwd, "rev-parse", "HEAD")
    if any(result is None or result.returncode != 0 for result in (root, branch, head)):
        return {"kind": "not a checkout"}
    assert root is not None and branch is not None and head is not None
    # The vault's snapshot job commits every minute or two and hanadocs-push pushes them,
    # so they never block a stop; counting them, no session in the vault reads as pushed.
    ahead_result = _git(cwd, "rev-list", "--count", "--invert-grep",
                        f"--grep=^{SNAPSHOT_COMMIT_PREFIX}", "@{u}..HEAD")
    upstream: Upstream = {"kind": "no upstream"}
    if ahead_result is not None and ahead_result.returncode == 0:
        try:
            upstream = {"kind": "tracking", "ahead": int(ahead_result.stdout.strip())}
        except ValueError:
            pass
    status = _git(cwd, "status", "--porcelain")
    dirty = [] if status is None or status.returncode else [
        line[3:] if len(line) >= 3 else line
        for line in status.stdout.splitlines()
        if line
    ]
    return {
        "kind": "git",
        "path": root.stdout.strip(),
        "head": (
            {"kind": "branch", "name": branch.stdout.strip()}
            if branch.stdout.strip()
            else {"kind": "detached", "commit": head.stdout.strip()}
        ),
        "upstream": upstream,
        "dirty": dirty,
    }


_LAST_STEP_BYTES = 262_144
_STEP_FIELDS = ("description", "command", "file_path", "pattern", "url", "prompt")
_STEP_TEXT = 200


def last_tool_call(config_dir: Path, session_id: str) -> str:
    """Name the transcript's last tool call as `<tool>: <its first descriptive input>`, or "" when none shows."""
    path = transcript_path(config_dir, session_id)
    if path is None:
        return ""
    try:
        with path.open("rb") as transcript:
            size = transcript.seek(0, os.SEEK_END)
            _ = transcript.seek(max(0, size - _LAST_STEP_BYTES))
            lines = transcript.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            entry = cast(object, json.loads(line))
        except ValueError:
            continue
        if not isinstance(entry, dict) or cast(dict[str, object], entry).get("type") != "assistant":
            continue
        message = cast(dict[str, object], entry).get("message")
        content = cast(dict[str, object], message).get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in reversed(cast(list[object], content)):
            if not isinstance(block, dict) or cast(dict[str, object], block).get("type") != "tool_use":
                continue
            call = cast(dict[str, object], block)
            name = call.get("name")
            arguments = call.get("input")
            detail = next((value for field in _STEP_FIELDS
                           if isinstance(arguments, dict)
                           and isinstance(value := cast(dict[str, object], arguments).get(field), str) and value), "")
            text = f"{name}: {detail}" if isinstance(name, str) and detail else name if isinstance(name, str) else ""
            return " ".join(text.split())[:_STEP_TEXT]
    return ""


def transcript_path(config_dir: Path, session_id: str) -> Path | None:
    """Find the newest main transcript, without visiting subagent directories."""
    newest: Path | None = None
    modified = float("-inf")
    try:
        for path in (config_dir / "projects").glob(f"*/{session_id}.jsonl"):
            try:
                at = path.stat().st_mtime
            except OSError:
                continue
            if at > modified:
                newest, modified = path, at
    except OSError:
        pass
    return newest


def last_model(path: Path) -> str | None:
    """Read complete transcript lines newest first, stopping at a real model."""
    try:
        with path.open("rb") as transcript:
            position = transcript.seek(0, os.SEEK_END)
            pending = b""
            while position:
                size = min(position, 64 * 1024)
                position -= size
                _ = transcript.seek(position)
                lines = (transcript.read(size) + pending).split(b"\n")
                pending = lines[0] if position else b""
                complete = lines[1:] if position else lines
                for line in reversed(complete):
                    try:
                        value = cast(object, json.loads(line))
                    except (ValueError, TypeError, UnicodeError):
                        continue
                    if not isinstance(value, dict):
                        continue
                    entry = cast(dict[str, object], value)
                    message = entry.get("message")
                    if entry.get("type") != "assistant" or not isinstance(message, dict):
                        continue
                    candidate = cast(dict[str, object], message).get("model")
                    if isinstance(candidate, str) and candidate != "<synthetic>":
                        return candidate
    except OSError:
        pass
    return None


def _model(session_id: str, config_dir: Path) -> LastModel:
    transcript = transcript_path(config_dir, session_id)
    model = last_model(transcript) if transcript is not None else None
    return (
        {"kind": "model", "name": model}
        if model is not None
        else {"kind": "no reply yet"}
    )


def _launcher_command(pid: int) -> list[str]:
    """Find the nearest live launcher ancestor without flattening argv boundaries."""
    try:
        darwin = _DarwinSnapshotProcesses().processes if sys.platform == "darwin" else {}
    except (OSError, herdr.HerdrFailure):
        return []
    visited: set[int] = set()
    while pid > 1 and pid not in visited and len(visited) < 64:
        visited.add(pid)
        if not _pid_alive(pid):
            return []
        try:
            if sys.platform == "linux":
                process = Path(os.environ.get("HERDR_PROC", "/proc")) / str(pid)
                argv = [os.fsdecode(part) for part in process.joinpath("cmdline").read_bytes().split(b"\0") if part]
                fields = process.joinpath("stat").read_text().rsplit(")", 1)[1].split()
                parent = int(fields[1])
            else:
                process = darwin.get(pid)
                if process is None or isinstance(process.arguments, herdr.UnreadableProcessArguments):
                    return []
                parent = process.parent
                argv = list(process.arguments.argv)
        except (OSError, ValueError, IndexError, UnicodeError, subprocess.SubprocessError):
            return []
        if any(Path(arg).name in {"implement.sh", "review.sh"} for arg in argv):
            return argv
        pid = parent
    return []


def _left_since_offered(directory: Path, roster: dict[str, dict[str, object]]) -> bool:
    """An earlier stop paused every worker, and the director has written no board line since.

    Each stop writes `<seat>.resume-prompt.md`, so the oldest one is the earliest offer
    the restart made. A board older than that offer means the director took none back.
    """
    if not roster or any(entry.get("status") != "paused" for entry in roster.values()):
        return False
    try:
        board = (directory / "board.log").stat().st_mtime
        offered = min((directory / f"{name}.resume-prompt.md").stat().st_mtime for name in roster)
    except OSError:
        return False
    return board < offered


def codex_seats(run_directories: list[Path]) -> list[PausedSeat]:
    from shutdown_record import PausedSeat

    seats: list[PausedSeat] = []
    for directory in run_directories:
        run_dir = str(directory)
        roster = {
            name: cast(dict[str, object], raw)
            for name, raw in sorted(_json_object(Path(run_dir) / "mesh_roster.json").items())
            if isinstance(raw, dict) and cast(dict[str, object], raw).get("status") != "ended"
        }
        # A run whose director left it is not one to resume: its marker outlived the work.
        if _left_since_offered(directory, roster):
            continue
        for name, entry in roster.items():
            thread_id, role = entry.get("thread_id", ""), entry.get("role", "impl")
            launcher = entry.get("launcher_pid", 0)
            turn_live = entry.get("status") in {"running", "starting"} or entry.get("paused_turn") is True
            seat = PausedSeat(run_dir=run_dir, seat=name,
                              role=role if isinstance(role, str) else "impl",
                              thread_id=thread_id if isinstance(thread_id, str) else "",
                              turn_live=turn_live,
                              launcher_command=_launcher_command(launcher) if isinstance(launcher, int) else [])
            seats.append(seat)
    return seats


def _codex_servers(run_directories: list[Path]) -> list[CodexServer]:
    servers: list[CodexServer] = []
    for directory in run_directories:
        record = _json_object(directory / "mesh_server.json")
        pid = _positive_int(record.get("pid"))
        if pid is None or not _pid_alive(pid):
            continue
        busy: list[str] = []
        for name, raw_entry in sorted(_json_object(directory / "mesh_roster.json").items()):
            if not isinstance(raw_entry, dict):
                continue
            entry = cast(dict[str, object], raw_entry)
            status = entry.get("status")
            launcher = _positive_int(entry.get("launcher_pid"))
            # Saved status alone does not prove a launcher is still doing work.
            launcher_is_live = launcher is not None and _pid_alive(launcher)
            if status in {"running", "starting"} and launcher_is_live:
                busy.append(name)
        servers.append({"run_dir": str(directory), "pid": pid, "busy_workers": busy})
    return servers


def _timers(session_id: str) -> list[str]:
    target = f"session:{session_id}"
    return [
        instance.name
        for instance in sorted(_notifier_state_dir().glob("*"))
        if instance.is_dir() and _conf(instance / "conf").get("TARGET") == target
    ]


def _machine() -> str:
    name = socket.gethostname().split(".", 1)[0]
    return name or ("mac" if sys.platform == "darwin" else "natedev")


def _listed_session(
    record: SessionFile,
    productions: dict[str, ProductionSession],
    units: dict[str, UnitMarks],
    home_state: account.LegacyHomeLogin | account.ConfigOnlyHome,
) -> ListedSession | UnattributedSession:
    pid = record.get("pid", 0)
    name = _string(record.get("name"))
    if not _record_matches_process(record):
        return {"pid": pid, "name": name, "reason": "process start mismatch"}
    attributed = _configuration_for_session(pid, home_state)
    if isinstance(attributed, UnreadableSessionConfiguration):
        return {"pid": pid, "name": name, "reason": "account unreadable"}
    session_id = _string(record.get("sessionId"))
    production = productions.get(session_id)
    unit = units.get(session_id)
    if production is not None:
        kind: Literal["showrunner", "unit", "worker", "top-level"] = "showrunner"
        slug, doc = production.slug, production.doc
    elif unit is not None:
        kind = "unit"
        slug, doc = unit.production, unit.doc
    else:
        kind = "worker" if record.get("kind") == "bg" else "top-level"
        slug, doc = "", ""
    return ListedSession(
        session_id=session_id,
        pid=pid,
        proc_start=_string(record.get("procStart")),
        name=name,
        kind=kind,
        config_dir=attributed.config_dir,
        login=attributed.account.login,
        production=slug,
        doc=doc,
    )


def listing(login: str | None = None, *,
            registrations: ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[ListedSession]:
    """List membership without inspecting transcripts, worktrees or host resources."""
    home_state = account.home_account_state()
    timers = showrunner_registrations() if isinstance(registrations, str) else registrations
    productions = {timer.session_id: ProductionSession(timer.slug, timer.doc) for timer in timers.entries}
    units = _unit_sessions(timers.entries)
    found: list[ListedSession] = []
    for record in sessions.live_sessions():
        entry = _listed_session(_session_file(record), productions, units, home_state)
        if isinstance(entry, ListedSession) and (
            login is None or entry.login.casefold() == login.casefold()
        ):
            found.append(entry)
    return found


def all_live_accounts() -> list[Inventory]:
    """Read current restore details for every locally attributed Claude login."""
    from concurrent.futures import ThreadPoolExecutor

    logins = sorted({item.login for item in listing()})
    if not logins:
        return []
    def read_account(login: str) -> Inventory:
        return inventory(login, refresh_desktops=False)

    with ThreadPoolExecutor(max_workers=len(logins)) as pool:
        return list(pool.map(read_account, logins))


@dataclass(frozen=True)
class ShutdownProcess:
    """A process identity and its recorded ownership at shutdown observation."""

    pid: int
    parent: int
    started: str
    arguments: tuple[str, ...] = ()
    session_id: str = ""
    run_dir: str = ""
    build_unit: str = ""
    process_session: int = 0
    observed_parents: frozenset[int] = frozenset()
    observed_groups: frozenset[int] = frozenset()
    ownership_unreadable: bool = False
    unreconciled_ownership_text: str = ""
    cgroups: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProcessObservation:
    """Discovered identities plus every unresolved read that prevents proving absence."""

    processes: dict[int, ShutdownProcess]
    errors: tuple[str, ...] = ()
    uncertain: frozenset[int] = frozenset()
    terminated: frozenset[tuple[int, str]] = frozenset()
    observed_identities: frozenset[tuple[int, str]] = frozenset()


@dataclass(frozen=True)
class ProcessLive:
    started: str


@dataclass(frozen=True)
class ProcessGone:
    """The process no longer exists (including a reaped/zombie identity)."""


@dataclass(frozen=True)
class ProcessUnknown:
    reason: str


ProcessIdentity = ProcessLive | ProcessGone | ProcessUnknown


def shutdown_identity(pid: int, timeout: float = 5) -> ProcessIdentity:
    try:
        if sys.platform == "darwin":
            shown = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,lstart="],
                capture_output=True, text=True, check=False, timeout=timeout,
                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
            if shown.returncode == 1 and not shown.stdout.strip():
                return ProcessGone()
            if shown.returncode:
                return ProcessUnknown(f"pid {pid}: ps exit {shown.returncode}: {shown.stderr.strip()}")
            status, separator, started = shown.stdout.strip().partition(" ")
            if not separator or not started.strip():
                return ProcessUnknown(f"pid {pid}: malformed ps identity")
            return ProcessGone() if status.startswith("Z") else ProcessLive(started.lstrip())
        fields = Path(os.environ.get("HERDR_PROC", "/proc"), str(pid), "stat").read_bytes().rpartition(b")")[2].split()
        return ProcessGone() if fields[0] == b"Z" else ProcessLive(fields[19].decode("ascii"))
    except FileNotFoundError:
        try:
            present = Path(os.environ.get("HERDR_PROC", "/proc"), str(pid)).exists()
        except OSError as error:
            return ProcessUnknown(f"pid {pid}: identity unread: {error}")
        return ProcessUnknown(f"pid {pid}: stat missing from existing process") if present else ProcessGone()
    except (OSError, subprocess.SubprocessError, IndexError, UnicodeError) as error:
        return ProcessUnknown(f"pid {pid}: identity unread: {error}")


def shutdown_server_identities(timeout: float, *, server_unit: str = "") -> frozenset[tuple[int, str]]:
    """Only the service's verified main process is a permanent server keep."""
    if sys.platform != "linux":
        return frozenset()
    expires = time.monotonic() + timeout
    try:
        shown = subprocess.run(["systemctl", "--user", "show", server_unit or herdr.server_unit(herdr.shared_session()),
            "--property=MainPID", "--value"], capture_output=True, text=True, check=False, timeout=timeout)
        if shown.returncode or not shown.stdout.strip().isdecimal():
            return frozenset()
        pid = int(shown.stdout.strip())
        remaining = expires - time.monotonic()
        if pid <= 0 or remaining <= 0:
            return frozenset()
        identity = shutdown_identity(pid, remaining)
        return frozenset({(pid, identity.started)}) if isinstance(identity, ProcessLive) else frozenset()
    except (OSError, subprocess.SubprocessError):
        return frozenset()


def _collect_process_table(arguments: list[str], timeout: float) -> tuple[str, int]:
    """Return the table and the exact collector pid, which is never a target."""
    with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}) as collector:
        try:
            output, error = collector.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            collector.kill()
            _ = collector.communicate()
            raise
        if collector.returncode:
            raise subprocess.CalledProcessError(collector.returncode, arguments, output, error)
        return output, collector.pid


def shutdown_processes(timeout: float = 5) -> ProcessObservation:
    """Observe ownership without turning failed or partial reads into absence."""
    result: dict[int, ShutdownProcess] = {}
    errors: list[str] = []
    if sys.platform == "darwin":
        tables: list[dict[int, tuple[ShutdownProcess, str, str]]] = []
        collectors: set[tuple[int, str]] = set()
        identity_errors: dict[tuple[int, str], list[str]] = {}
        for flags in (("-axww",), ("eww", "-axww")):
            try:
                output, collector = _collect_process_table(
                    ["ps", *flags, "-o", "pid=,ppid=,pgid=,stat=,lstart=,command="], max(0.001, timeout / 2))
            except (OSError, subprocess.SubprocessError) as error:
                return ProcessObservation(result, (f"ps observation: {error}",))
            table: dict[int, tuple[ShutdownProcess, str, str]] = {}
            for line in output.splitlines():
                row = re.fullmatch(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+\s+\S+\s+\d+\s+\d+:\d+:\d+\s+\d+)\s*(.*)", line)
                if row is None:
                    errors.append(f"ps malformed row: {line[:160]}")
                    continue
                pid, parent, group = map(int, row.groups()[:3])
                status, started, text = row.groups()[3:]
                row_errors = identity_errors.setdefault((pid, started), [])
                ownership_unreadable = bool(re.fullmatch(r"\([^()]*\)", text))
                if ownership_unreadable:
                    row_errors.append(f"pid {pid}: argv unread: ps comm fallback")
                try:
                    arguments = tuple(shlex.split(text))
                except ValueError:
                    # ps renders argv as text, not shell syntax. Quotes do not
                    # erase numeric ancestry or the raw environment suffix.
                    arguments = ()
                table[pid] = (ShutdownProcess(pid, parent, started, arguments, process_session=group,
                    ownership_unreadable=ownership_unreadable,
                    unreconciled_ownership_text=text if tables else ""), status, text)
            # This collector produced its own row while still alive. Only that
            # exact identity is exempt; a later reuse of its pid is ordinary.
            if collector in table:
                collectors.add((collector, table[collector][0].started))
            tables.append(table)
        uncertain: set[int] = set()
        terminated: set[tuple[int, str]] = set()
        for pid in tables[0].keys() | tables[1].keys():
            first = tables[0].get(pid)
            later = tables[1].get(pid)
            latest = later if later is not None else first
            assert latest is not None
            process, status, text = latest
            if (pid, process.started) in collectors:
                continue
            # Reconcile identity before status. Later Z proves this identity dead;
            # it never proves an earlier/different identity alive.
            if status.startswith("Z"):
                gone = {(pid, row[0].started) for row in (first, later) if row is not None}
                terminated.update(gone)
                for identity in gone:
                    _ = identity_errors.pop(identity, None)
                continue
            result[pid] = process
            if first is None or later is None or first[0].started != later[0].started:
                uncertain.add(pid)
                errors.append(f"pid {pid}: unmatched process identity between tables")
                continue
            parents = frozenset({first[0].parent, process.parent})
            groups = frozenset({first[0].process_session, process.process_session})
            process = ShutdownProcess(pid, process.parent, process.started, process.arguments,
                process_session=process.process_session, observed_parents=parents, observed_groups=groups,
                ownership_unreadable=first[0].ownership_unreadable or process.ownership_unreadable)
            result[pid] = process
            argv = first[2]
            if not text.startswith(argv) or (len(text) > len(argv) and not text[len(argv)].isspace()):
                uncertain.add(pid)
                result[pid] = ShutdownProcess(pid, process.parent, process.started, process.arguments,
                    process_session=process.process_session, observed_parents=parents, observed_groups=groups,
                    ownership_unreadable=process.ownership_unreadable, unreconciled_ownership_text=text)
                errors.append(f"pid {pid}: environment prefix does not match argv identity")
                continue
            tail = text[len(argv):]
            entries = list(re.finditer(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", tail))
            ownership_keys = {"CLAUDE_CODE_SESSION_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR"}
            # ps does not encode environment value boundaries. A repeated key,
            # or a marker after another value, might be text inside that value.
            ambiguous = any(entry[1] in ownership_keys and (index != 0 or
                bool(tail[:entry.start()].strip()) or sum(other[1] == entry[1] for other in entries) != 1)
                for index, entry in enumerate(entries))
            if ambiguous:
                uncertain.add(pid)
                errors.append(f"pid {pid}: ownership marker boundary unknown")
                result[pid] = ShutdownProcess(pid, process.parent, process.started, first[0].arguments,
                    process_session=process.process_session, observed_parents=parents, observed_groups=groups,
                    ownership_unreadable=True, unreconciled_ownership_text=text)
                continue
            environment = {entry[1]: tail[entry.end():entries[index + 1].start()
                if index + 1 < len(entries) else len(tail)].rstrip()
                for index, entry in enumerate(entries)}
            result[pid] = ShutdownProcess(pid, process.parent, process.started, first[0].arguments,
                environment.get("CLAUDE_CODE_SESSION_ID", ""),
                environment.get("UNIT_RUN_DIR") or environment.get("UNIT_SESSION_DIR", ""),
                process_session=process.process_session, observed_parents=parents, observed_groups=groups,
                ownership_unreadable=process.ownership_unreadable)
        errors.extend(error for key, entries in identity_errors.items() if key not in collectors for error in entries)
        identities = frozenset((pid, row[0].started) for table in tables for pid, row in table.items()
                               if (pid, row[0].started) not in collectors)
        return ProcessObservation(result, tuple(errors), frozenset(uncertain), frozenset(terminated), identities)
    root = Path(os.environ.get("HERDR_PROC", "/proc"))
    try:
        paths = list(root.iterdir())
    except OSError as error:
        return ProcessObservation(result, (f"process table unread: {error}",))
    for process in paths:
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            fields = process.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                continue
            arguments = tuple(os.fsdecode(word) for word in process.joinpath("cmdline").read_bytes().split(b"\0") if word)
            environment: dict[str, str] = {}
            cgroups: list[tuple[str, Path]] = []
            ownership_unreadable = False
            try:
                environment = _snapshot_environment(process)
            except OSError as error:
                errors.append(f"pid {process.name}: environment unread: {error}")
            try:
                cgroups = herdr.systemd_cgroups(process)
            except OSError as error:
                errors.append(f"pid {process.name}: cgroup unread: {error}")
                ownership_unreadable = True
            scope = next((group.name for _hierarchy, group in cgroups
                          if "builds.slice" in group.parts and group.suffix in {".scope", ".service"}), "")
            pid = int(process.name)
            result[pid] = ShutdownProcess(pid, int(fields[1]), fields[19], arguments,
                environment.get("CLAUDE_CODE_SESSION_ID", ""),
                environment.get("UNIT_RUN_DIR") or environment.get("UNIT_SESSION_DIR", ""), scope, int(fields[3]),
                ownership_unreadable=ownership_unreadable,
                cgroups=frozenset(str(group) for _hierarchy, group in cgroups))
        except FileNotFoundError:
            try:
                if process.exists():
                    errors.append(f"pid {process.name}: required process file missing")
            except OSError as error:
                errors.append(f"pid {process.name}: observation unread: {error}")
            continue
        except (OSError, ValueError, IndexError) as error:
            errors.append(f"pid {process.name}: observation unread: {error}")
    return ProcessObservation(result, tuple(errors))


def inventory(
    login: str,
    only: frozenset[str] = frozenset(),  # pyright: ignore[reportCallInDefaultInitializer]
    *, refresh_desktops: bool = True,
) -> Inventory:
    """Return account sessions in ``only`` and their workers, or all when empty."""
    home_state = account.home_account_state()
    records = [_session_file(record) for record in sessions.live_sessions()]
    timers = showrunner_registrations()
    productions = {timer.session_id: ProductionSession(timer.slug, timer.doc) for timer in timers.entries}
    units = _unit_sessions(timers.entries)
    session_ids = {
        session_id
        for record in records
        for session_id in [_string(record.get("sessionId"))]
        if session_id
    }
    run_directories = _active_run_directories()
    worker_owners = _worker_owners(run_directories, session_ids)
    wanted_login = login.casefold()
    candidates = [
        (record, _listed_session(record, productions, units, home_state))
        for record in records
        if not only or _string(record.get("sessionId")) in only
        or worker_owners.get(_string(record.get("sessionId"))) in only
    ]
    has_sessions = any(
        isinstance(entry, ListedSession) and entry.login.casefold() == wanted_login
        for _, entry in candidates
    )
    plans = _newest_unit_plans() if has_sessions else {}
    desktops = (
        (_snapshot_desktops() if refresh_desktops else _snapshot_desktops(refresh=False))
        if has_sessions else {}
    )
    found: list[Session] = []
    unattributed: list[UnattributedSession] = []

    for record, entry in candidates:
        if not isinstance(entry, ListedSession):
            unattributed.append(entry)
            continue
        if entry.login.casefold() != wanted_login:
            continue

        session_id = entry.session_id
        found.append(_session_details(record, entry, directories=run_directories.get(session_id, []),
            host=_host(record, units.get(session_id), plans, desktops), timers=_timers(session_id),
            environment=_process_environment(entry.pid), worker_owner=DirectorOwner(kind="director", session_id=worker_owners[session_id]) if session_id in worker_owners else NoDirector(kind="no director")))

    return {
        "machine": _machine(),
        "login": login,
        "label": label_for("claude", login),
        "sessions": found,
        "unattributed": unattributed,
    }


def _session_details(record: SessionFile, entry: ListedSession, *, directories: list[Path],
                     host: Host, timers: list[str], environment: dict[str, str], worker_owner: WorkerOwner) -> Session:
    cwd = _string(record.get("cwd"))
    common: dict[str, object] = {
        "session_id": entry.session_id, "pid": entry.pid, "proc_start": entry.proc_start,
        "name": entry.name, "cwd": cwd, "config_dir": str(entry.config_dir),
        "probe": environment.get(PROBE_ENV) == "1", "status": _string(record.get("status"), "unknown"),
        "updated_at": _wire_integer(record.get("updatedAt", 0), "session.updatedAt"),
        "model": _model(entry.session_id, entry.config_dir), "checkout": _checkout(cwd),
        "run_dirs": [str(directory) for directory in directories],
        "codex_servers": _codex_servers(directories), "timers": timers, "host": host,
        "kind": "top-level" if entry.kind == "unit" and host["kind"] == "unknown" else entry.kind,
    }
    if entry.kind == "showrunner":
        common.update(production=entry.production, doc=entry.doc)
    elif entry.kind == "worker":
        common["owner"] = worker_owner
    return parse_session(common, "observed session", require_updated_at=True)


class ObservationFailure(OSError):
    """One required source could not establish a complete observation."""


@dataclass(frozen=True)
class _SnapshotTimers:
    registrations: tuple[ShowrunnerRegistration, ...]
    targets: dict[str, list[str]]
    enabled: dict[str, bool]


def _snapshot_timers() -> _SnapshotTimers:
    root = _notifier_state_dir()
    try:
        instances = list(root.iterdir())
    except FileNotFoundError:
        return _SnapshotTimers((), {}, {})
    registrations: list[ShowrunnerRegistration] = []
    targets: dict[str, list[str]] = {}
    enabled: dict[str, bool] = {}
    for instance in instances:
        if not stat.S_ISDIR(instance.stat().st_mode):
            continue
        conf = instance / "conf"
        try:
            fields = dict(line.split("=", 1) for line in conf.read_text().splitlines() if "=" in line)
        except FileNotFoundError:
            if instance.name.startswith("showrunner-"):
                raise ObservationFailure(f"production registration: {conf} is missing") from None
            continue
        target = fields.get("TARGET", "")
        if target.startswith("session:") and target[8:]:
            targets.setdefault(target[8:], []).append(instance.name)
        try:
            enabled[instance.name] = "ENABLED=1" in (instance / "state").read_text().splitlines()
        except FileNotFoundError:
            enabled[instance.name] = False
        if not instance.name.startswith("showrunner-"):
            continue
        try:
            command = shlex.split(fields.get("CHECK", ""))
            index = next(index for index, token in enumerate(command) if Path(token).name == "production_check.sh")
            doc = Path(command[index + 1])
            if not doc.is_absolute() or not target.startswith("session:") or not target[8:]:
                raise ValueError("missing production doc or session target")
            _ = doc.read_text()
        except (StopIteration, IndexError, ValueError) as error:
            raise ObservationFailure(f"production registration {conf}: {error}") from error
        registrations.append(ShowrunnerRegistration(target[8:], instance.name.removeprefix("showrunner-"), str(doc)))
    return _SnapshotTimers(tuple(registrations), targets, enabled)


def _snapshot_timer_restores(owner: Session, timers: _SnapshotTimers) -> list[TimerRestore]:
    try:
        pause = conversation_pause.read_record(conversation_pause.record_path(owner["session_id"]))
    except FileNotFoundError:
        pause = conversation_pause.NoPauseRecord.ABSENT
    restores: dict[str, TimerRestore] = {}
    if isinstance(pause, conversation_pause.PauseRecord):
        restore = not isinstance(pause.phase, conversation_pause.KeptOff)
        for instance in pause.instances:
            restores[instance] = TimerRestore(instance=instance, was_enabled=restore)
    for instance in owner["timers"]:
        _ = restores.setdefault(instance, TimerRestore(instance=instance,
            was_enabled=timers.enabled.get(instance, False)))
    return list(restores.values())


def _snapshot_environment(process: Path) -> dict[str, str]:
    return dict(os.fsdecode(item).split("=", 1) for item in process.joinpath("environ").read_bytes().split(b"\0")
                if b"=" in item)


def _snapshot_stat(process: Path) -> tuple[str, int]:
    fields = process.joinpath("stat").read_bytes().rpartition(b")")[2].split()
    try:
        started = fields[19].decode("ascii")
        if not started.isdecimal():
            raise ValueError("invalid process start")
        return started, int(fields[4])
    except (IndexError, ValueError, UnicodeError) as error:
        raise ObservationFailure(f"process liveness {process}: malformed stat") from error


class _SnapshotProcesses:
    """Strict Linux observations; absence and unreadability remain distinct."""

    def __init__(self) -> None:
        self.root: Path = Path(os.environ.get("HERDR_PROC", "/proc"))

    def boot(self) -> str:
        value = self.root.joinpath("sys/kernel/random/boot_id").read_text().strip()
        if not value:
            raise ObservationFailure("boot id is empty")
        return value

    def paths(self) -> list[Path]:
        return [path for path in self.root.iterdir() if path.name.isdecimal()]

    def path(self, pid: int) -> Path:
        return self.root / str(pid)

    def exists(self, process: Path) -> bool:
        return process.exists()

    def identity(self, process: Path) -> tuple[str, int]:
        return _snapshot_stat(process)

    def environment(self, process: Path) -> dict[str, str]:
        return _snapshot_environment(process)

    def command(self, process: Path) -> str:
        return process.joinpath("comm").read_text().strip() if process.stat().st_uid == os.getuid() else ""

    def arguments(self, process: Path) -> list[str]:
        return [os.fsdecode(item) for item in process.joinpath("cmdline").read_bytes().split(b"\0") if item]

    def cwd(self, process: Path) -> str:
        return str(process.joinpath("cwd").resolve(strict=True))

    def rollout(self, process: Path, home: Path) -> CodexSessionId | CodexResumePicker:
        return _codex_resume(process, home)

    def started_at(self, process: Path) -> datetime:
        started, _ = self.identity(process)
        for line in (self.root / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return datetime.fromtimestamp(int(line.split()[1]) +
                    int(started) / os.sysconf("SC_CLK_TCK"), timezone.utc)
        raise ObservationFailure("process start: boot time is missing")

    def servers(self, processes: list[Path]) -> dict[str, RecoveryHostProcess]:
        found: dict[str, RecoveryHostProcess] = {}
        for pid, name in herdr._live_server_processes(processes).items():  # pyright: ignore[reportPrivateUsage]
            started, _ = self.identity(self.path(pid))
            found[name] = RecoveryHostProcess(kind="process", pid=pid, proc_start=started)
        return found


class _DarwinSnapshotProcesses(_SnapshotProcesses):
    """One user-process observation, with native identity and file readers."""

    def __init__(self) -> None:
        super().__init__()
        self.processes: dict[int, herdr._UserProcess] = {  # pyright: ignore[reportPrivateUsage]
            process.pid: process for process in herdr._darwin_processes()}  # pyright: ignore[reportPrivateUsage]
        self.identities: dict[int, tuple[str, int]] = {}

    @override
    def boot(self) -> str:
        program = os.environ.get("SHUTDOWN_SYSCTL", "sysctl")
        result = herdr.run_program([program, "-n", "kern.bootsessionuuid"])
        herdr.require_success(result, "boot session uuid")
        value = result.stdout.strip()
        if not value:
            raise ObservationFailure("boot session uuid is empty")
        return value

    @override
    def paths(self) -> list[Path]:
        return [Path(str(pid)) for pid in self.processes]

    @override
    def path(self, pid: int) -> Path:
        return Path(str(pid))

    @override
    def exists(self, process: Path) -> bool:
        return int(process.name) in self.processes

    @override
    def identity(self, process: Path) -> tuple[str, int]:
        pid = int(process.name)
        if pid not in self.processes:
            raise FileNotFoundError(f"process {pid} is absent")
        if pid not in self.identities:
            self.identities[pid] = herdr.darwin_process_identity(pid)
        return self.identities[pid]

    @override
    def environment(self, process: Path) -> dict[str, str]:
        return herdr.darwin_process_environment(int(process.name))

    @override
    def command(self, process: Path) -> str:
        return Path(self.processes[int(process.name)].executable).name

    @override
    def arguments(self, process: Path) -> list[str]:
        arguments = self.processes[int(process.name)].arguments
        if isinstance(arguments, herdr.UnreadableProcessArguments):
            raise ObservationFailure(arguments.reason)
        return list(arguments.argv)

    @override
    def cwd(self, process: Path) -> str:
        cwd = herdr._darwin_process_cwd(int(process.name))  # pyright: ignore[reportPrivateUsage]
        if isinstance(cwd, herdr.UnreadableCwd):
            raise ObservationFailure(cwd.detail)
        return cwd

    @override
    def rollout(self, process: Path, home: Path) -> CodexSessionId | CodexResumePicker:
        ids: set[str] = set()
        for path in herdr.darwin_open_files(int(process.name)):
            if path.is_relative_to(home / "sessions"):
                match = _ROLLOUT_ID.fullmatch(path.name)
                if match:
                    ids.add(match[1].lower())
        if len(ids) > 1:
            raise ObservationFailure(f"Codex rollout {process}: more than one open session id")
        return CodexSessionId(kind="session id", session_id=next(iter(ids))) if ids else CodexResumePicker(kind="picker")

    @override
    def started_at(self, process: Path) -> datetime:
        started, _ = self.identity(process)
        # darwin_process_identity reads lstart with LC_ALL=C and TZ=UTC.
        return datetime.strptime(started, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)

    @override
    def servers(self, processes: list[Path]) -> dict[str, RecoveryHostProcess]:
        config = herdr._session_socket("default").parent  # pyright: ignore[reportPrivateUsage]
        found: dict[str, RecoveryHostProcess] = {}
        for listener in herdr._darwin_bound_sockets():  # pyright: ignore[reportPrivateUsage]
            if listener.pid not in self.processes or listener.path.name != "herdr.sock":
                continue
            if listener.path == config / "herdr.sock":
                name = "default"
            elif listener.path.parent.parent == config / "sessions":
                name = listener.path.parent.name
            else:
                continue
            started, _ = self.identity(self.path(listener.pid))
            if name in found and found[name]["pid"] != listener.pid:
                raise ObservationFailure(f"herdr {name}: multiple socket holders")
            found[name] = RecoveryHostProcess(kind="process", pid=listener.pid, proc_start=started)
        return found

    def terminal(self, pid: int, desktop: Desktop) -> tuple[SessionHost, RecoveryHostProcess | RecoveryHostUnrecorded]:
        current, child = pid, 0
        supervisor: SupervisorIdentity | NoSupervisor = NoSupervisor(kind="no supervisor")
        parent = self.processes.get(self.processes[pid].parent)
        if parent is not None and isinstance(parent.arguments, herdr.ReadProcessArguments) and any(
                Path(argument).name == "relaunch.py" and
                parent.arguments.argv[index + 1:index + 3] == ("run", "--")
                for index, argument in enumerate(parent.arguments.argv)):
            started, _ = self.identity(self.path(parent.pid))
            supervisor = SupervisorIdentity(kind="supervisor", pid=parent.pid, proc_start=started)
        seen: set[int] = set()
        while current > 1 and current not in seen:
            seen.add(current)
            process = self.processes.get(current)
            if process is None:
                raise ObservationFailure(f"terminal ancestry {current}: process is missing")
            kind = terminal_kind(process.executable)
            if kind != "unknown":
                started, _ = self.identity(self.path(current))
                identity = RecoveryHostProcess(kind="process", pid=current, proc_start=started)
                if (kind == "ghostty" or kind == "zed") and child:
                    return WindowHost(kind=kind, window_shell=child, desktop=desktop,
                        supervisor=supervisor if supervisor.get("pid") != child else NoSupervisor(kind="no supervisor")), identity
                if kind == "terminal":
                    if child and supervisor["kind"] == "supervisor" and supervisor["pid"] != child:
                        return TerminalHost(kind="terminal", supervisor=supervisor), identity
                    return TerminalHost(kind="terminal"), identity
                return UnknownHost(kind="unknown"), identity
            child, current = current, process.parent
        return UnknownHost(kind="unknown"), RecoveryHostUnrecorded(kind="not recorded")


def _snapshot_processes() -> _SnapshotProcesses:
    return _DarwinSnapshotProcesses() if sys.platform == "darwin" else _SnapshotProcesses()


@dataclass(frozen=True)
class _SnapshotHosts:
    native: dict[int, herdr.HerdrPlace]
    presentation: dict[str, Literal["attached window", "detached"]]
    desktops: dict[str, str]
    servers: dict[str, RecoveryHostProcess]
    reader: _SnapshotProcesses


def _snapshot_native_hosts() -> tuple[dict[int, herdr.HerdrPlace], dict[str, Literal["attached window", "detached"]]]:
    running = herdr.live_sessions()
    places = {process.pid: process.place for process in herdr.herdr_processes()}
    presentation: dict[str, Literal["attached window", "detached"]] = {}
    for session in running:
        clients = herdr.attached_clients(session["name"])
        if isinstance(clients, herdr.ClientsUnreadable):
            raise ObservationFailure(f"herdr clients {session['name']}: {clients.detail}")
        presentation[session["name"]] = "attached window" if isinstance(clients, herdr.ClientsAttached) else "detached"
    return places, presentation


def _snapshot_host(pid: int, session_id: str, environment: dict[str, str], hosts: _SnapshotHosts) -> SessionHost:
    if environment.get("TMUX_PANE"):
        return UnknownHost(kind="unknown")
    if environment.get("HERDR_PANE_ID"):
        place = hosts.native.get(pid)
        if place is None or place.session not in hosts.presentation:
            raise ObservationFailure(f"herdr host for pid {pid}: pane could not be resolved")
        return HerdrHost(kind="herdr", herdr_session=place.session, pane_id=place.pane,
                         presentation=hosts.presentation[place.session])
    desktop: Desktop = (NamedDesktop(kind="named", name=hosts.desktops[session_id])
                        if session_id in hosts.desktops else DesktopNotInSnapshot(kind="not in snapshot"))
    if isinstance(hosts.reader, _DarwinSnapshotProcesses):
        return hosts.reader.terminal(pid, desktop)[0]
    return _terminal_host(pid, desktop)


def _snapshot_units(timers: _SnapshotTimers) -> dict[str, UnitMarks]:
    every = unit_lookup.every_marked_unit()
    if every.faults:
        raise ObservationFailure("production unit marks: " + "; ".join(
            f"{slug}: {every.production_fault(slug)}" for slug in sorted(every.faults)))
    docs = {runner.slug: runner.doc for runner in timers.registrations}
    units: dict[str, UnitMarks] = {}
    for slug, marked in every.units.items():
        for unit, row in marked.items():
            if isinstance(row.claude, unit_lookup.ClaudeUnknown):
                raise ObservationFailure(f"production {slug}/{unit}: {row.claude.reason}")
            if not isinstance(row.claude, unit_lookup.LiveClaude):
                raise ObservationFailure(f"production {slug}/{unit}: marked host has no independently confirmed Claude identity")
            if slug not in docs:
                raise ObservationFailure(f"production {slug}: live unit has no registered production doc")
            units[row.claude.session_id] = UnitMarks(slug, unit, docs[slug], row.place.session, "herdr")
    return units


_CODEX_HEADLESS = frozenset({"exec", "app-server", "mcp", "mcp-server", "proto"})
_CODEX_VALUE_OPTIONS = frozenset({"--config", "-c", "--model", "-m", "--profile", "-p", "--cd", "-C",
                                  "--sandbox", "-s", "--ask-for-approval", "-a", "--enable", "--disable"})
_ROLLOUT_ID = re.compile(r"^rollout-.*-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$")


def _codex_interactive(arguments: list[str]) -> bool:
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in _CODEX_VALUE_OPTIONS:
            index += 2
        elif argument.startswith("-"):
            index += 1
        else:
            return argument not in _CODEX_HEADLESS
    return True


class CodexDescriptorsUnreadable(ObservationFailure):
    """The open-rollout identity could not be observed."""


def _codex_resume(process: Path, home: Path) -> CodexSessionId | CodexResumePicker:
    try:
        descriptors = list((process / "fd").iterdir())
    except OSError as error:
        raise CodexDescriptorsUnreadable(f"Codex descriptors {process}: {error}") from error
    ids: set[str] = set()
    for descriptor in descriptors:
        try:
            path = Path(os.readlink(descriptor))
        except OSError:
            continue
        if not path.is_relative_to(home / "sessions"):
            continue
        match = _ROLLOUT_ID.fullmatch(path.name)
        if match:
            ids.add(match[1].lower())
    if len(ids) > 1:
        raise ObservationFailure(f"Codex rollout {process}: more than one open session id")
    return CodexSessionId(kind="session id", session_id=next(iter(ids))) if ids else CodexResumePicker(kind="picker")


class _CodexSessionMetadata(TypedDict):
    id: str
    cwd: str
    timestamp: str


def _codex_rollout_metadata(line: str) -> _CodexSessionMetadata:
    value = _wire_object(cast(object, json.loads(line)), "Codex rollout")
    if value.get("type") != "session_meta":
        raise ValueError("Codex rollout does not begin with session_meta")
    payload = _wire_object(value.get("payload"), "Codex session_meta")
    return _CodexSessionMetadata(id=_wire_string(payload.get("id"), "Codex session_meta.id"),
        cwd=_wire_string(payload.get("cwd"), "Codex session_meta.cwd"),
        timestamp=_wire_string(payload.get("timestamp"), "Codex session_meta.timestamp"))


def _codex_rollout_paths(root: Path, first_day: date) -> Iterator[Path]:
    """Enumerate relevant folders without glob's suppression of read errors."""
    try:
        years = list(root.iterdir())
    except FileNotFoundError:
        return
    except OSError as error:
        raise ObservationFailure(f"Codex sessions {root}: {error}") from error
    try:
        for year in years:
            if not year.name.isdecimal() or int(year.name) < first_day.year or not stat.S_ISDIR(year.stat().st_mode):
                continue
            for month in year.iterdir():
                if not month.name.isdecimal() or not stat.S_ISDIR(month.stat().st_mode):
                    continue
                if (int(year.name), int(month.name)) < (first_day.year, first_day.month):
                    continue
                for day in month.iterdir():
                    if not day.name.isdecimal() or not stat.S_ISDIR(day.stat().st_mode):
                        continue
                    try:
                        candidate_day = date(int(year.name), int(month.name), int(day.name))
                    except ValueError:
                        continue
                    if candidate_day >= first_day:
                        yield from day.iterdir()
    except OSError as error:
        raise ObservationFailure(f"Codex sessions {root}: {error}") from error


def _codex_disk_resume(home: Path, cwd: str, started: datetime) -> CodexSessionId | CodexResumePicker:
    """Consider only local day folders and the first line of each rollout."""
    ids: set[str] = set()
    for rollout in _codex_rollout_paths(home / "sessions", started.astimezone().date()):
        match = _ROLLOUT_ID.fullmatch(rollout.name)
        if not match:
            continue
        try:
            with rollout.open(encoding="utf-8") as handle:
                metadata = _codex_rollout_metadata(handle.readline())
            timestamp = datetime.fromisoformat(metadata["timestamp"])
        except (FileNotFoundError, ValueError, UnicodeError):
            # A vanished or unfinished rollout is not evidence of an identity.
            continue
        if (timestamp.tzinfo is not None and timestamp >= started and
                metadata["cwd"] == cwd and metadata["id"].lower() == match[1].lower()):
            ids.add(metadata["id"].lower())
            if len(ids) > 1:
                return CodexResumePicker(kind="picker")
    return CodexSessionId(kind="session id", session_id=next(iter(ids))) if ids else CodexResumePicker(kind="picker")


def _observed_codex_resume(reader: _SnapshotProcesses, process: Path, home: Path,
                           cwd: str) -> CodexSessionId | CodexResumePicker:
    try:
        resume = reader.rollout(process, home)
    except CodexDescriptorsUnreadable:
        resume = CodexResumePicker(kind="picker")
    if resume["kind"] == "session id":
        return resume
    return _codex_disk_resume(home, cwd, reader.started_at(process))


def _snapshot_codex(processes: list[Path], hosts: _SnapshotHosts, at: str) -> dict[str, list[SnapshotSession]]:
    reader = hosts.reader
    found: dict[str, list[SnapshotSession]] = {}
    homes: dict[str, Path] = {}
    for process in processes:
        try:
            if reader.command(process) != "codex":
                continue
            started, terminal = reader.identity(process)
            arguments = reader.arguments(process)
            if not terminal or not arguments or not _codex_interactive(arguments):
                continue
            environment = reader.environment(process)
            home = Path(environment.get("CODEX_HOME") or Path.home() / ".codex").absolute()
            cwd = reader.cwd(process)
            resume = _observed_codex_resume(reader, process, home, cwd)
            identity = resume["session_id"] if resume["kind"] == "session id" else f"codex-{process.name}-{started}"
            login = "codex-" + home.name.casefold()
            if login in homes and homes[login] != home:
                raise ObservationFailure(f"Codex home collision: {homes[login]} and {home}")
            homes[login] = home
            owner = TopLevelSession(kind="top-level", session_id=identity, pid=int(process.name), proc_start=started,
                name=f"codex-{process.name}", cwd=cwd, probe=False, status="unknown", updated_at=0,
                model=NoReplyYet(kind="no reply yet"), checkout=_checkout(cwd), run_dirs=[], codex_servers=[], timers=[],
                host=_snapshot_host(int(process.name), identity, environment, hosts))
            found.setdefault(login, []).append(SnapshotCodexSession(agent="codex", session=owner,
                codex=CodexLaunch(home=str(home), resume=resume), workers=[], timers=[], last_seen=at, state="running"))
        except FileNotFoundError:
            if reader.exists(process):
                raise ObservationFailure(f"Codex observation {process}: required process data is missing") from None
    return found


def _snapshot_host_process(owner: Session, hosts: _SnapshotHosts) -> RecoveryHostProcess | RecoveryHostUnrecorded:
    """Save the terminal process identity while its ancestry is still available."""
    host = owner["host"]
    reader = hosts.reader
    if host["kind"] == "herdr":
        return hosts.servers.get(host["herdr_session"], RecoveryHostUnrecorded(kind="not recorded"))
    if host["kind"] == "unit" and host["session_host"] == "herdr":
        return hosts.servers.get(host["session_name"], RecoveryHostUnrecorded(kind="not recorded"))
    if host["kind"] not in {"ghostty", "zed", "terminal"}:
        return RecoveryHostUnrecorded(kind="not recorded")
    if isinstance(reader, _DarwinSnapshotProcesses):
        return reader.terminal(owner["pid"], DesktopNotInSnapshot(kind="not in snapshot"))[1]
    proc = Path(os.environ.get("HERDR_PROC", "/proc"))
    current = owner["pid"]
    seen: set[int] = set()
    while current > 1 and current not in seen:
        seen.add(current)
        process = proc / str(current)
        try:
            command = process.joinpath("comm").read_text().strip()
            if terminal_kind(command) != "unknown":
                started, _ = reader.identity(process)
                return RecoveryHostProcess(kind="process", pid=current, proc_start=started)
            fields = process.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            current = int(fields[1])
        except FileNotFoundError:
            if reader.exists(process):
                raise ObservationFailure(f"host ancestry {process}: required process data is missing") from None
            break
        except (IndexError, ValueError) as error:
            raise ObservationFailure(f"host ancestry {process}: {error}") from error
    return RecoveryHostUnrecorded(kind="not recorded")


def _snapshot_gone_hosts(hosts: _SnapshotHosts, boot_id: str) -> frozenset[tuple[str, str]]:
    """Positive host loss is distinct from an unavailable host identity."""
    from shutdown_record import all_snapshots
    gone: set[tuple[str, str]] = set()
    reader = hosts.reader
    for saved in all_snapshots():
        for entry in saved["sessions"]:
            if entry.get("boot_id") != boot_id or entry["state"] == "unresolved":
                continue
            host = entry["session"]["host"]
            lost = False
            identity = entry.get("host_process", RecoveryHostUnrecorded(kind="not recorded"))
            if identity["kind"] == "process":
                process = reader.path(identity["pid"])
                try:
                    started, _ = reader.identity(process)
                    lost = started != identity["proc_start"]
                except FileNotFoundError:
                    if reader.exists(process):
                        raise ObservationFailure(f"host process {identity['pid']}: stat is missing") from None
                    lost = True
            elif host["kind"] == "herdr":
                lost = host["herdr_session"] not in hosts.presentation
            elif host["kind"] == "unit" and host["session_host"] == "herdr":
                lost = host["session_name"] not in hosts.presentation
            if lost:
                gone.add((saved["login"], entry["session"]["session_id"]))
    return frozenset(gone)


def snapshot_inventory() -> CompleteInventory | PartialInventory:
    """Observe every account once, without changing timers, pauses or vault notes."""
    failures: list[str] = []
    registry = sessions.read_registry()
    failures.extend(f"session registry: {path} is unreadable" for path in registry.unreadable)
    try:
        reader = _snapshot_processes()
        boot_id = reader.boot()
        home_state = account.home_account_state()
        timers = _snapshot_timers()
        units = _snapshot_units(timers)
        native, presentation = _snapshot_native_hosts()
        herdr.save_shared_layout(set(presentation))
        _ = detection_rules.refresh(set(presentation), herdr.reload_agent_rules)
        desktops = _snapshot_desktops(refresh=False)
        processes = reader.paths()
        # Socket ownership also distinguishes a replacement with the same session name.
        servers = reader.servers(processes)
        if isinstance(reader, _DarwinSnapshotProcesses):
            missing = set(presentation) - servers.keys()
            if missing:
                raise ObservationFailure("herdr socket owners missing: " + ", ".join(sorted(missing)))
        hosts = _SnapshotHosts(native, presentation, desktops, servers, reader)
        run_directories = _active_run_directories()
        ids = {item.record["sessionId"] for item in registry.entries}
        worker_owners = _worker_owners(run_directories, ids)
        plans = _newest_unit_plans()
        gone_hosts = _snapshot_gone_hosts(hosts, boot_id)
    except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
        return PartialInventory(tuple([*failures, f"shared observations: {error}"]))
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    by_login: dict[str, list[Session]] = {}
    labels: dict[str, str] = {}
    productions = {timer.session_id: ProductionSession(timer.slug, timer.doc) for timer in timers.registrations}
    for item in registry.entries:
        registered, details = item.record, item.details
        process = reader.path(registered["pid"])
        try:
            started, _ = reader.identity(process)
            if registered.get("procStart") and registered.get("procStart") != started:
                continue  # A positively different process is no longer this session.
            if not registered.get("procStart"):
                raise ObservationFailure(f"session {registered['sessionId']}: recorded process identity is missing")
            data = cast(SessionFile, cast(object, {**registered, **details}))
            if isinstance(reader, _DarwinSnapshotProcesses):
                data["cwd"] = reader.cwd(process)
            environment = reader.environment(process)
            configured = environment.get("CLAUDE_CONFIG_DIR", "")
            setting = account.ConfiguredClaudeFolder(Path(configured)) if configured else account.NoAccountSetting()
            configuration = _configuration_from_setting(setting, home_state)
            if isinstance(configuration, UnreadableSessionConfiguration):
                raise ObservationFailure(f"account attribution pid {registered['pid']}: configuration is unreadable")
            config, attributed = configuration.config_dir, configuration.account
            production, unit = productions.get(registered["sessionId"]), units.get(registered["sessionId"])
            kind: Literal["showrunner", "unit", "worker", "top-level"] = ("showrunner" if production else "unit" if unit else
                "worker" if data.get("kind") == "bg" else "top-level")
            entry = ListedSession(registered["sessionId"], registered["pid"], started, registered["name"], kind,
                config, attributed.login.casefold(), production.slug if production else "", production.doc if production else "")
            host: Host = (_host(data, unit, plans, desktops) if unit else
                          _snapshot_host(entry.pid, entry.session_id, environment, hosts))
            owner = _session_details(data, entry, directories=run_directories.get(entry.session_id, []), host=host,
                timers=timers.targets.get(entry.session_id, []), environment=environment,
                worker_owner=DirectorOwner(kind="director", session_id=worker_owners[entry.session_id]) if entry.session_id in worker_owners else NoDirector(kind="no director"))
            by_login.setdefault(entry.login, []).append(owner)
            labels[entry.login] = attributed.label
        except FileNotFoundError as error:
            if reader.exists(process):
                failures.append(f"session {registered['sessionId']}: {error}")
        except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
            failures.append(f"session {registered['sessionId']}: {error}")
    snapshots: dict[str, list[SnapshotSession]] = {}
    all_owners = [owner for owners in by_login.values() for owner in owners]
    owner_ids = {owner["session_id"] for owner in all_owners if owner["kind"] != "worker"}
    live_ids = [owner["session_id"] for owner in all_owners]
    if len(live_ids) != len(set(live_ids)):
        failures.append("session registry: duplicate live session identities")
    for login, owners in by_login.items():
        for owner in owners:
            if owner["kind"] == "worker":
                if owner["owner"]["kind"] != "director" or owner["owner"]["session_id"] not in owner_ids:
                    failures.append(f"worker {owner['session_id']}: owner could not be established")
                continue
            try:
                workers: list[Session] = [worker for worker in all_owners if worker["kind"] == "worker" and
                           worker["owner"]["kind"] == "director" and worker["owner"]["session_id"] == owner["session_id"]]
                snapshots.setdefault(login, []).append(SnapshotClaudeSession(agent="claude", session=owner,
                    workers=workers, timers=_snapshot_timer_restores(owner, timers), last_seen=at, state="running",
                    boot_id=boot_id, host_process=_snapshot_host_process(owner, hosts)))
            except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
                failures.append(f"timer/pause intent {owner['session_id']}: {error}")
    try:
        for login, entries in _snapshot_codex(processes, hosts, at).items():
            for item in entries:
                item["boot_id"] = boot_id
                item["host_process"] = _snapshot_host_process(item["session"], hosts)
            snapshots[login] = entries
            labels[login] = login
    except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
        failures.append(f"Codex observation: {error}")
    return PartialInventory(tuple(failures)) if failures else CompleteInventory(_machine(), snapshots, labels, KnownBoot(boot_id), gone_hosts)


@dataclass(frozen=True)
class CodexLive:
    pid: int


@dataclass(frozen=True)
class CodexGone:
    """No current interactive Codex has the saved identity."""


@dataclass(frozen=True)
class CodexLivenessUnknown:
    reason: str


def codex_liveness(entry: SnapshotCodexSession) -> CodexLive | CodexGone | CodexLivenessUnknown:
    """Check PID identity and rollout IDs independently of the saved account."""
    try:
        reader = _snapshot_processes()
        processes = reader.paths()
    except (OSError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
        return CodexLivenessUnknown(str(error))
    failure = ""
    for process in processes:
        try:
            if reader.command(process) != "codex":
                continue
            started, terminal = reader.identity(process)
            argv = reader.arguments(process)
            if not terminal or not argv or not _codex_interactive(argv):
                continue
            if int(process.name) == entry["session"]["pid"] and started == entry["session"]["proc_start"]:
                return CodexLive(int(process.name))
            if entry["codex"]["resume"]["kind"] != "session id":
                continue
            environment = reader.environment(process)
            home = Path(environment.get("CODEX_HOME") or Path.home() / ".codex").absolute()
            resume = reader.rollout(process, home)
            if resume == entry["codex"]["resume"]:
                return CodexLive(int(process.name))
        except FileNotFoundError as error:
            if reader.exists(process):
                failure = str(error)
        except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
            failure = str(error)
    return CodexLivenessUnknown(failure) if failure else CodexGone()
