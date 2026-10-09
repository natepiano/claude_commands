#!/usr/bin/env python3
"""Inventory the live Claude work belonging to one account on this machine."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "production"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import sessions
import showrunners
import unit_lookup
from account import Account, account_of, label_for


class Host(TypedDict):
    """Where a session runs and the information needed to bring it back."""

    kind: Literal["unit", "tmux", "ghostty", "zed", "terminal", "unknown"]
    production: NotRequired[str]
    unit: NotRequired[str]
    doc: NotRequired[str]
    plan: NotRequired[str]
    tmux_session: NotRequired[str]
    desktop: NotRequired[str]
    window_shell: NotRequired[int]


class Checkout(TypedDict):
    """The current committed and uncommitted state of a session checkout."""

    path: str
    branch: str
    head: str
    ahead: int | None
    dirty: list[str]


class CodexServer(TypedDict):
    """A live Codex app-server owned by one Claude session's run folder."""

    run_dir: str
    pid: int
    busy_seats: list[str]


class Session(TypedDict):
    """One live Claude session attributed to the requested account."""

    session_id: str
    pid: int
    name: str
    cwd: str
    kind: Literal["showrunner", "unit", "seat", "top-level"]
    status: str
    host: Host
    model: str | None
    checkout: Checkout | None
    run_dirs: list[str]
    codex_servers: list[CodexServer]
    timers: list[str]
    owner: str | None


class Inventory(TypedDict):
    """Everything this phase can safely attribute to one account on one machine."""

    machine: str
    login: str
    label: str
    sessions: list[Session]
    unknown: list[str]


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
class UnitSession:
    production: str
    unit: str
    doc: str
    tmux_session: str


DESKTOP_HEADING = re.compile(r"^##\s+desktop:\s*(.+?)\s*$")
SNAPSHOT_SESSION = re.compile(r"^`(.+)` · (\S+) · (.+?)\s*$")
RESUME_ID = re.compile(r"(?:^|\s)--resume\s+['\"]?([^\s'\"]+)")


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


def _delegate_root() -> Path:
    return Path(os.environ.get("SHUTDOWN_DELEGATE_ROOT", "/tmp/claude/delegate"))


def _session_file(record: sessions.SessionRecord) -> SessionFile:
    """Add fields that the shared routing reader deliberately does not retain."""
    data = cast(dict[str, object], cast(object, record)).copy()
    pid = record["pid"]
    path = sessions.sessions_dir() / f"{pid}.json"
    data.update(_json_object(path))
    return cast(SessionFile, cast(object, data))


def _configuration_for_session(pid: int) -> Account | None:
    """Keep account lookup at one boundary so an unreadable process stays unattributed."""
    try:
        return account_of(pid)
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _process_start(pid: int) -> str | None:
    """The process identity stored in session records, in this platform's native units."""
    if sys.platform == "darwin":
        # Claude writes `procStart` from `ps` under this locale and zone; any other text never matches.
        try:
            result = subprocess.run(
                ("ps", "-o", "lstart=", "-p", str(pid)),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        started = result.stdout.strip()
        return started if result.returncode == 0 and started else None
    try:
        fields = (
            Path(f"/proc/{pid}/stat")
            .read_text(encoding="utf-8")
            .rpartition(")")[2]
            .split()
        )
        return fields[19]
    except (OSError, IndexError):
        return None


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


def _registered_productions() -> tuple[dict[str, ProductionSession], list[showrunners.Showrunner]]:
    state_dir = _notifier_state_dir()
    try:
        runners = showrunners.registered_showrunners(state_dir)
    except OSError:
        runners = []
    found: dict[str, ProductionSession] = {}
    for runner in runners:
        instance = state_dir / f"showrunner-{runner['slug']}"
        fields = _conf(instance / "conf")
        target = fields.get("TARGET", "")
        if not target.startswith("session:") or not target[8:]:
            continue
        found[target[8:]] = ProductionSession(runner["slug"], runner["doc"])
    return found, runners


def _unit_sessions(runners: list[showrunners.Showrunner]) -> dict[str, UnitSession]:
    found: dict[str, UnitSession] = {}
    for runner in runners:
        slug = runner["slug"]
        try:
            marked = unit_lookup.marked_units(slug)
        except OSError:
            continue
        for unit_id, marked_unit in marked.items():
            claude = marked_unit.claude
            if not isinstance(claude, unit_lookup.LiveClaude):
                continue
            found[claude.session_id] = UnitSession(
                production=slug,
                unit=unit_id,
                doc=runner["doc"],
                tmux_session=marked_unit.label,
            )
    return found


def _active_run_directories() -> dict[str, list[Path]]:
    """Every director-to-run mapping, including directors that have already exited."""
    found: dict[str, list[Path]] = {}
    active = _delegate_root() / "active"
    try:
        mappings = sorted(active.iterdir())
    except OSError:
        return found
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


def _seat_owners(run_directories: dict[str, list[Path]], session_ids: set[str]) -> dict[str, str]:
    short_to_session = {session_id[:8].casefold(): session_id for session_id in session_ids}
    owners: dict[str, str] = {}
    for owner, directories in run_directories.items():
        for directory in directories:
            try:
                lines = (directory / "seats").read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                daemon_short = line.split("\t", 1)[0].strip().casefold()
                seat = short_to_session.get(daemon_short)
                if seat is not None:
                    owners[seat] = owner
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
            result = subprocess.run(
                ("ps", "eww", "-o", "command=", "-p", str(pid)),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if result.returncode:
            return {}
        tokens = result.stdout.split()
        pairs = (token.split("=", 1) for token in tokens if "=" in token)
        return {name: value for name, value in pairs}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    entries = (os.fsdecode(entry).split("=", 1) for entry in raw.split(b"\0") if b"=" in entry)
    return {name: value for name, value in entries}


def _tmux_session(pane: str) -> str:
    if not pane:
        return ""
    try:
        result = subprocess.run(
            (unit_lookup.tmux_binary(), "display", "-p", "-t", pane, "#S"),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _terminal_kind(command: str) -> Literal["ghostty", "zed", "terminal", "unknown"]:
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


def _terminal_host(pid: int) -> Host:
    seen: set[int] = set()
    current = pid
    child = 0
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
        terminal = _terminal_kind(fields[1])
        if terminal != "unknown":
            host: Host = {"kind": terminal}
            if child > 0:
                host["window_shell"] = child
            return host
        child = current
        current = parent
    return {"kind": "unknown"}


def _snapshot_desktops() -> dict[str, str]:
    if sys.platform == "darwin":
        return {}
    snapshotter = shutil.which("agent-sessions-snapshot")
    if snapshotter is not None:
        try:
            _ = subprocess.run(
                (snapshotter,),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
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
    showrunner: ProductionSession | None,
    unit: UnitSession | None,
    plans: dict[str, str],
    desktops: dict[str, str],
) -> Host:
    session_id = _string(session.get("sessionId"))
    if unit is not None:
        host: Host = {
            "kind": "unit",
            "production": unit.production,
            "unit": unit.unit,
            "doc": unit.doc,
        }
        tmux_session = unit.tmux_session or _string(session.get("tmux")).partition(":")[0]
        if tmux_session:
            host["tmux_session"] = tmux_session
        plan = plans.get(session_id)
        if plan is not None:
            host["plan"] = plan
        return host

    environment = _process_environment(session.get("pid", 0))
    pane = environment.get("TMUX_PANE", "")
    tmux_session = _tmux_session(pane)
    if pane:
        host = {"kind": "tmux"}
        if tmux_session:
            host["tmux_session"] = tmux_session
    else:
        host = _terminal_host(session.get("pid", 0))
        desktop = desktops.get(session_id)
        if host["kind"] in {"ghostty", "zed"} and desktop is not None:
            host["desktop"] = desktop
    if showrunner is not None and showrunner.doc:
        host["doc"] = showrunner.doc
    return host


def _git(cwd: str, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ("git", "-C", cwd, *arguments),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _checkout(cwd: str) -> Checkout | None:
    if not cwd:
        return None
    root = _git(cwd, "rev-parse", "--show-toplevel")
    branch = _git(cwd, "branch", "--show-current")
    head = _git(cwd, "rev-parse", "HEAD")
    if any(result is None or result.returncode != 0 for result in (root, branch, head)):
        return None
    assert root is not None and branch is not None and head is not None
    ahead_result = _git(cwd, "rev-list", "--count", "@{u}..HEAD")
    ahead: int | None = None
    if ahead_result is not None and ahead_result.returncode == 0:
        try:
            ahead = int(ahead_result.stdout.strip())
        except ValueError:
            ahead = None
    status = _git(cwd, "status", "--porcelain")
    dirty = [] if status is None or status.returncode else [
        line[3:] if len(line) >= 3 else line
        for line in status.stdout.splitlines()
        if line
    ]
    return {
        "path": root.stdout.strip(),
        "branch": branch.stdout.strip(),
        "head": head.stdout.strip(),
        "ahead": ahead,
        "dirty": dirty,
    }


def _model(session_id: str) -> str | None:
    project_root = Path.home() / ".claude/projects"
    try:
        matches = list(project_root.rglob(f"{session_id}.jsonl"))
    except OSError:
        return None
    if not matches:
        return None
    try:
        transcript = max(matches, key=lambda path: path.stat().st_mtime)
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    model: str | None = None
    for line in lines:
        try:
            value = cast(object, json.loads(line))
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        entry = cast(dict[str, object], value)
        if entry.get("type") != "assistant" or not isinstance(entry.get("message"), dict):
            continue
        candidate = cast(dict[str, object], entry["message"]).get("model")
        if isinstance(candidate, str):
            model = candidate
    return model


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
            launcher_is_live = "launcher_pid" not in entry or (
                launcher is not None and _pid_alive(launcher)
            )
            if status in {"running", "starting"} and launcher_is_live:
                busy.append(name)
        servers.append({"run_dir": str(directory), "pid": pid, "busy_seats": busy})
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


def inventory(
    login: str,
    only: frozenset[str] = frozenset(),  # pyright: ignore[reportCallInDefaultInitializer]
) -> Inventory:
    """Return the live sessions safely attributable to ``login`` on this machine."""
    records = [_session_file(record) for record in sessions.live_sessions()]
    session_ids = {
        session_id
        for record in records
        for session_id in [_string(record.get("sessionId"))]
        if session_id
    }
    run_directories = _active_run_directories()
    seat_owners = _seat_owners(run_directories, session_ids)
    productions, runners = _registered_productions()
    units = _unit_sessions(runners)
    plans = _newest_unit_plans()
    desktops = _snapshot_desktops()
    wanted_login = login.casefold()
    found: list[Session] = []
    unknown: list[str] = []

    for record in records:
        pid = record.get("pid", 0)
        session_id = _string(record.get("sessionId"))
        name = _string(record.get("name"))
        owner = seat_owners.get(session_id)
        selected = not only or session_id in only or owner in only
        if not selected:
            continue
        if not _record_matches_process(record):
            unknown.append(f"{pid} {name}")
            continue
        attributed = _configuration_for_session(pid)
        if attributed is None:
            unknown.append(f"{pid} {name}")
            continue
        if attributed.login.casefold() != wanted_login:
            continue

        production = productions.get(session_id)
        unit = units.get(session_id)
        raw_kind = _string(record.get("kind"))
        if production is not None:
            kind: Literal["showrunner", "unit", "seat", "top-level"] = "showrunner"
        elif unit is not None:
            kind = "unit"
        elif raw_kind == "bg":
            kind = "seat"
        else:
            kind = "top-level"
        directories = run_directories.get(session_id, [])
        cwd = _string(record.get("cwd"))
        found.append({
            "session_id": session_id,
            "pid": pid,
            "name": name,
            "cwd": cwd,
            "kind": kind,
            "status": _string(record.get("status"), "unknown"),
            "host": _host(record, production, unit, plans, desktops),
            "model": _model(session_id),
            "checkout": _checkout(cwd),
            "run_dirs": [str(directory) for directory in directories],
            "codex_servers": _codex_servers(directories),
            "timers": _timers(session_id),
            "owner": owner if kind == "seat" else None,
        })

    return {
        "machine": _machine(),
        "login": login,
        "label": label_for("claude", login),
        "sessions": found,
        "unknown": unknown,
    }
