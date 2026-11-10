#!/usr/bin/env python3
"""Inventory the live Claude work belonging to one account on this machine."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import json
import os
import shlex
import socket
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast

from scripts.direct.assign_gate import WORKER_LAUNCHERS
from scripts.whoami import account
from scripts.agents import remote
from scripts.lib.probe_session import PROBE_ENV
from scripts.herdr import herdr
from scripts.message import sessions
from scripts.message.sessions import process_start as _process_start
from scripts.production import showrunners, staff, session_staff
from scripts.usage import agents_log
from scripts.direct.worker_state import worker_status_path
from scripts.lib.resume_message import ClaudeChildResume, Resumable, Idle, Unavailable, WorkerResume
from scripts.production import unit_lookup
from scripts.whoami.account import Account, label_for


from scripts.agents.session_types import (
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
    SessionFields as SessionFields,
    ShowrunnerSession as ShowrunnerSession,
    UnitSession as UnitSession,
    WorkerSession as WorkerSession,
    TopLevelSession as TopLevelSession,
    Session as Session,
    TimerRestore as TimerRestore,
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
    name: str
    doc: str


@dataclass(frozen=True)
class ShowrunnerRegistration:
    session_id: str
    name: str
    doc: str


@dataclass(frozen=True)
class ShowrunnerRegistrations:
    entries: tuple[ShowrunnerRegistration, ...]
    retired_sessions: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class UnitMarks:
    production: str
    unit: str
    doc: str
    session_name: str
    session_host: Literal["herdr"]


def parse_inventory(text: str) -> Inventory:
    """Decode and recursively validate one JSON inventory."""
    try:
        value = cast(object, json.loads(text))
    except (ValueError, TypeError):
        raise InvalidInventory("inventory is not valid JSON") from None
    data = _wire_object(value, "inventory")
    parsed_sessions = [
        parse_session(item, f"inventory.sessions[{index}]")
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
    record = staff.snapshot()
    for instance in sorted(_notifier_state_dir().glob("showrunner-*")):
        if not instance.is_dir():
            continue
        fields = _conf(instance / "conf")
        target = fields.get("TARGET", "")
        if not target.startswith("session:") or not target[8:]:
            continue
        owner = record.by_id(staff.StaffId(instance.name.removeprefix("showrunner-")))
        if (owner is None or owner.kind != "showrunner"
                or not isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc))
                or owner.doc.machine != remote.local_machine()):
            continue
        doc = owner.doc
        try:
            _ = doc.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        found.append(ShowrunnerRegistration(target[8:], owner.name, str(doc.path)))
    retired = frozenset(str(identity) for row in record.rows if isinstance(row.worktree, staff.RemovedWorktree)
                        for identity in (row.id, row.current_session))
    return ShowrunnerRegistrations(tuple(found), retired)


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
    return [showrunners.Showrunner(name=timer.name, doc=timer.doc, session="", socket="", zone="")
            for timer in timers.entries if timer.session_id not in live_ids]


def _unit_sessions(runners: tuple[ShowrunnerRegistration, ...]) -> dict[str, UnitMarks]:
    """Each running unit director of a registered production, by session id.

    Read the native session labels once for all productions.
    Omit productions that have no showrunner timer or have faulty session labels.
    """
    try:
        every = unit_lookup.every_labeled_session()
    except OSError:
        return {}
    docs = {runner.name: runner.doc for runner in runners}
    found: dict[str, UnitMarks] = {}
    for name, units in every.units.items():
        if name not in docs:
            continue
        for unit_id, labeled_session in units.items():
            claude = labeled_session.claude
            if not isinstance(claude, unit_lookup.LiveClaude):
                continue
            found[claude.session_id] = UnitMarks(
                production=name,
                unit=unit_id,
                doc=docs[name],
                session_name=labeled_session.place.session,
                session_host="herdr",
            )
    return found


def _active_run_directories() -> dict[str, list[Path]]:
    """Every director-to-run mapping, including directors that have already exited."""
    found: dict[str, list[Path]] = {}
    for row in staff.local_snapshot().rows:
        if isinstance(row.run, staff.OpenRun):
            found.setdefault(row.current_session, []).append(row.run.path.expanduser())
    return found


def _worker_owners(run_directories: dict[str, list[Path]], session_ids: set[str]) -> dict[str, str]:
    membership = staff.local_snapshot()
    return {row.current_session: parent.current_session for row in membership.find(kind="worker")
            if row.current_session in session_ids and (parent := membership.parent_of(row)) is not None
            and parent.current_session in run_directories}


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


def _host(
    session: SessionFile,
    unit: UnitMarks | None,
    plans: dict[str, str],
) -> Host:
    session_id = _string(session.get("sessionId"))
    environment = _process_environment(session.get("pid", 0))
    if environment.get("TMUX_PANE"):
        return {"kind": "unknown"}
    if unit is not None:
        plan = plans.get(session_id)
        return {
            "kind": "unit",
            "unit": unit.unit,
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
    desktop: Desktop = {"kind": "not in snapshot"}
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
    ahead_result = _git(cwd, "rev-list", "--count", "@{u}..HEAD")
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
    """Read the nearest live launcher ancestor and preserve argument boundaries."""
    visited: set[int] = set()
    try:
        darwin = {process.pid: process for process in herdr._darwin_processes()} if sys.platform == "darwin" and pid > 1 else {}  # pyright: ignore[reportPrivateUsage]
        while pid > 1 and pid not in visited and len(visited) < 64 and _pid_alive(pid):
            visited.add(pid)
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
            if any(Path(arg).name in WORKER_LAUNCHERS for arg in argv):
                return argv
            pid = parent
    except (OSError, ValueError, IndexError, subprocess.SubprocessError, herdr.HerdrFailure):
        return []
    return []


def worker_resumes(parent: staff.StaffRow, membership: staff.StaffSnapshot) -> list[WorkerResume]:
    """Return one resume result for each Claude child."""
    children = [child for child in membership.find(kind="worker", parent=parent.id)
                if isinstance(membership.backends.get(child.id), staff.ClaudeBackend)]
    if not children:
        return []
    try:
        tasks = agents_log.assignments(agents_log.state_dir() / "agents.jsonl")
        moment = datetime.now(timezone.utc)
        runs = session_staff.worker_run_entries(membership, children)
    except (OSError, ValueError) as error:
        return [Unavailable(child.id, child.name, str(error).replace("\n", " ")) for child in children]
    results: list[WorkerResume] = []
    for child in children:
        try:
            found = next(reversed(runs.get(child.id, ())), None)
            if found is None:
                raise ValueError("Claude child record is unavailable in its recorded runs")
            directory, entry = found
            task = agents_log.role_at(tasks, child.current_session, moment)
            if not isinstance(task, agents_log.Assignment):
                raise ValueError("last assignment is unavailable" + (f": {'; '.join(tasks.problems)}" if tasks.problems else ""))
            status = worker_status_path(directory, str(entry["slot"])).read_text().strip()
            live = status == "implementing"
            launcher = entry.get("launcher_pid", 0)
            seat = ClaudeChildResume(run_dir=str(directory), seat=child.name, role=task.role, staff_id=child.id, turn_live=live,
                              launcher_command=_launcher_command(launcher) if isinstance(launcher, int) else [])
            results.append(Resumable(seat) if live else Idle(seat))
        except (OSError, ValueError) as error:
            results.append(Unavailable(child.id, child.name, str(error).replace("\n", " ")))
    return results


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
        name, doc = production.name, production.doc
    elif unit is not None:
        kind = "unit"
        name, doc = unit.production, unit.doc
    else:
        kind = "worker" if record.get("kind") == "bg" else "top-level"
        name, doc = "", ""
    return ListedSession(
        session_id=session_id,
        pid=pid,
        proc_start=_string(record.get("procStart")),
        name=name,
        kind=kind,
        config_dir=attributed.config_dir,
        login=attributed.account.login,
        production=name,
        doc=doc,
    )


def listing(login: str | None = None, *,
            registrations: ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> list[ListedSession]:
    """List membership without inspecting transcripts, worktrees or host resources."""
    home_state = account.home_account_state()
    timers = showrunner_registrations() if isinstance(registrations, str) else registrations
    productions = {timer.session_id: ProductionSession(timer.name, timer.doc) for timer in timers.entries}
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
        return inventory(login)

    with ThreadPoolExecutor(max_workers=len(logins)) as pool:
        return list(pool.map(read_account, logins))


def inventory(
    login: str,
    only: frozenset[str] = frozenset(),  # pyright: ignore[reportCallInDefaultInitializer]
) -> Inventory:
    """Return account sessions in ``only`` and their workers, or all when empty."""
    home_state = account.home_account_state()
    records = [_session_file(record) for record in sessions.live_sessions()]
    timers = showrunner_registrations()
    productions = {timer.session_id: ProductionSession(timer.name, timer.doc) for timer in timers.entries}
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
        if _string(record.get("sessionId")) not in timers.retired_sessions
        and (not only or _string(record.get("sessionId")) in only
             or worker_owners.get(_string(record.get("sessionId"))) in only)
    ]
    has_sessions = any(
        isinstance(entry, ListedSession) and entry.login.casefold() == wanted_login
        for _, entry in candidates
    )
    plans = _newest_unit_plans() if has_sessions else {}
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
            host=_host(record, units.get(session_id), plans), timers=_timers(session_id),
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
        "model": _model(entry.session_id, entry.config_dir), "checkout": {"kind": "not a checkout"},
        "run_dirs": [str(directory) for directory in directories],
        "timers": timers, "host": host,
        "kind": "top-level" if entry.kind == "unit" and host["kind"] == "unknown" else entry.kind,
    }
    if entry.kind == "showrunner":
        common.update(production=entry.production, doc=entry.doc)
    elif entry.kind == "worker":
        common["owner"] = worker_owner
    session = parse_session(common, "observed session")
    folder = session["cwd"]
    session["checkout"] = _checkout(folder) if isinstance(folder, str) else NotACheckout(kind="not a checkout")
    return session
