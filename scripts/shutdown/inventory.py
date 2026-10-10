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
from typing import Literal, TypedDict, cast

SCRIPTS = Path(__file__).resolve().parents[1]
for dependency in ("whoami", "message", "production"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import sessions
import showrunners
import unit_lookup
from account import Account, account_of, label_for


class PlanFromRunRecord(TypedDict):
    kind: Literal["plan"]
    path: str


class NoRunRecord(TypedDict):
    kind: Literal["no run record"]


UnitPlan = PlanFromRunRecord | NoRunRecord


class UnitHost(TypedDict):
    kind: Literal["unit"]
    production: str
    unit: str
    doc: str
    tmux_session: str
    plan: UnitPlan


class TmuxHost(TypedDict):
    kind: Literal["tmux"]
    tmux_session: str


class NamedDesktop(TypedDict):
    kind: Literal["named"]
    name: str


class DesktopNotInSnapshot(TypedDict):
    kind: Literal["not in snapshot"]


Desktop = NamedDesktop | DesktopNotInSnapshot


class WindowHost(TypedDict):
    kind: Literal["ghostty", "zed"]
    window_shell: int
    desktop: Desktop


class TerminalHost(TypedDict):
    kind: Literal["terminal"]


class UnknownHost(TypedDict):
    kind: Literal["unknown"]


Host = UnitHost | TmuxHost | WindowHost | TerminalHost | UnknownHost
SessionHost = TmuxHost | WindowHost | TerminalHost | UnknownHost


class OnBranch(TypedDict):
    kind: Literal["branch"]
    name: str


class DetachedHead(TypedDict):
    kind: Literal["detached"]
    commit: str


Head = OnBranch | DetachedHead


class Tracking(TypedDict):
    kind: Literal["tracking"]
    ahead: int


class NoUpstream(TypedDict):
    kind: Literal["no upstream"]


Upstream = Tracking | NoUpstream


class GitCheckout(TypedDict):
    kind: Literal["git"]
    path: str
    head: Head
    upstream: Upstream
    dirty: list[str]


class NotACheckout(TypedDict):
    kind: Literal["not a checkout"]


CheckoutState = GitCheckout | NotACheckout


class ModelName(TypedDict):
    kind: Literal["model"]
    name: str


class NoReplyYet(TypedDict):
    kind: Literal["no reply yet"]


LastModel = ModelName | NoReplyYet


class DirectorOwner(TypedDict):
    kind: Literal["director"]
    session_id: str


class NoDirector(TypedDict):
    kind: Literal["no director"]


SeatOwner = DirectorOwner | NoDirector


class CodexServer(TypedDict):
    """A live Codex app-server owned by one Claude session's run folder."""

    run_dir: str
    pid: int
    busy_seats: list[str]


class SessionFields(TypedDict):
    session_id: str
    pid: int
    proc_start: str
    name: str
    cwd: str
    status: str
    model: LastModel
    checkout: CheckoutState
    run_dirs: list[str]
    codex_servers: list[CodexServer]
    timers: list[str]


class ShowrunnerSession(SessionFields):
    kind: Literal["showrunner"]
    host: SessionHost
    production: str
    doc: str


class UnitSession(SessionFields):
    kind: Literal["unit"]
    host: UnitHost


class SeatSession(SessionFields):
    kind: Literal["seat"]
    host: SessionHost
    owner: SeatOwner


class TopLevelSession(SessionFields):
    kind: Literal["top-level"]
    host: SessionHost


Session = ShowrunnerSession | UnitSession | SeatSession | TopLevelSession


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


class InvalidInventory(ValueError):
    """An inventory value does not satisfy its wire contract."""


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
class UnitMarks:
    production: str
    unit: str
    doc: str
    tmux_session: str


DESKTOP_HEADING = re.compile(r"^##\s+desktop:\s*(.+?)\s*$")
SNAPSHOT_SESSION = re.compile(r"^`(.+)` · (\S+) · (.+?)\s*$")
RESUME_ID = re.compile(r"(?:^|\s)--resume\s+['\"]?([^\s'\"]+)")


def _wire_object(value: object, place: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidInventory(f"{place} must be an object")
    return cast(dict[str, object], value)


def _wire_required(data: dict[str, object], key: str, place: str) -> object:
    if key not in data:
        raise InvalidInventory(f"{place}.{key} is missing")
    return data[key]


def _wire_string(value: object, place: str) -> str:
    if not isinstance(value, str):
        raise InvalidInventory(f"{place} must be a string")
    return value


def _wire_integer(value: object, place: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidInventory(f"{place} must be an integer")
    return value


def _wire_strings(value: object, place: str) -> list[str]:
    if not isinstance(value, list):
        raise InvalidInventory(f"{place} must be a list")
    values = cast(list[object], value)
    if not all(isinstance(item, str) for item in values):
        raise InvalidInventory(f"{place} must contain only strings")
    return cast(list[str], values)


def _wire_list(value: object, place: str) -> list[object]:
    if not isinstance(value, list):
        raise InvalidInventory(f"{place} must be a list")
    return cast(list[object], value)


def _wire_kind(data: dict[str, object], place: str) -> str:
    return _wire_string(_wire_required(data, "kind", place), f"{place}.kind")


def _parse_unit_plan(value: object, place: str) -> UnitPlan:
    data = _wire_object(value, place)
    kind = _wire_kind(data, place)
    if kind == "plan":
        return {
            "kind": "plan",
            "path": _wire_string(
                _wire_required(data, "path", place), f"{place}.path"
            ),
        }
    if kind == "no run record":
        return {"kind": "no run record"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_desktop(value: object, place: str) -> Desktop:
    data = _wire_object(value, place)
    kind = _wire_kind(data, place)
    if kind == "named":
        return {
            "kind": "named",
            "name": _wire_string(
                _wire_required(data, "name", place), f"{place}.name"
            ),
        }
    if kind == "not in snapshot":
        return {"kind": "not in snapshot"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_host(value: object, place: str, allow_unit: bool) -> Host:
    data = _wire_object(value, place)
    kind = _wire_kind(data, place)
    if kind == "unit" and allow_unit:
        return {
            "kind": "unit",
            "production": _wire_string(
                _wire_required(data, "production", place), f"{place}.production"
            ),
            "unit": _wire_string(
                _wire_required(data, "unit", place), f"{place}.unit"
            ),
            "doc": _wire_string(_wire_required(data, "doc", place), f"{place}.doc"),
            "tmux_session": _wire_string(
                _wire_required(data, "tmux_session", place),
                f"{place}.tmux_session",
            ),
            "plan": _parse_unit_plan(
                _wire_required(data, "plan", place), f"{place}.plan"
            ),
        }
    if kind == "tmux":
        return {
            "kind": "tmux",
            "tmux_session": _wire_string(
                _wire_required(data, "tmux_session", place),
                f"{place}.tmux_session",
            ),
        }
    if kind in {"ghostty", "zed"}:
        window = {
            "kind": kind,
            "window_shell": _wire_integer(
                _wire_required(data, "window_shell", place),
                f"{place}.window_shell",
            ),
            "desktop": _parse_desktop(
                _wire_required(data, "desktop", place), f"{place}.desktop"
            ),
        }
        return cast(WindowHost, cast(object, window))
    if kind == "terminal":
        return {"kind": "terminal"}
    if kind == "unknown":
        return {"kind": "unknown"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_head(value: object, place: str) -> Head:
    data = _wire_object(value, place)
    kind = _wire_kind(data, place)
    if kind == "branch":
        return {
            "kind": "branch",
            "name": _wire_string(
                _wire_required(data, "name", place), f"{place}.name"
            ),
        }
    if kind == "detached":
        return {
            "kind": "detached",
            "commit": _wire_string(
                _wire_required(data, "commit", place), f"{place}.commit"
            ),
        }
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_upstream(value: object, place: str) -> Upstream:
    data = _wire_object(value, place)
    kind = _wire_kind(data, place)
    if kind == "tracking":
        return {
            "kind": "tracking",
            "ahead": _wire_integer(
                _wire_required(data, "ahead", place), f"{place}.ahead"
            ),
        }
    if kind == "no upstream":
        return {"kind": "no upstream"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_checkout(value: object, place: str) -> CheckoutState:
    data = _wire_object(value, place)
    kind = _wire_kind(data, place)
    if kind == "git":
        return {
            "kind": "git",
            "path": _wire_string(
                _wire_required(data, "path", place), f"{place}.path"
            ),
            "head": _parse_head(
                _wire_required(data, "head", place), f"{place}.head"
            ),
            "upstream": _parse_upstream(
                _wire_required(data, "upstream", place), f"{place}.upstream"
            ),
            "dirty": _wire_strings(
                _wire_required(data, "dirty", place), f"{place}.dirty"
            ),
        }
    if kind == "not a checkout":
        return {"kind": "not a checkout"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_model(value: object, place: str) -> LastModel:
    data = _wire_object(value, place)
    kind = _wire_kind(data, place)
    if kind == "model":
        return {
            "kind": "model",
            "name": _wire_string(
                _wire_required(data, "name", place), f"{place}.name"
            ),
        }
    if kind == "no reply yet":
        return {"kind": "no reply yet"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_owner(value: object, place: str) -> SeatOwner:
    data = _wire_object(value, place)
    kind = _wire_kind(data, place)
    if kind == "director":
        return {
            "kind": "director",
            "session_id": _wire_string(
                _wire_required(data, "session_id", place), f"{place}.session_id"
            ),
        }
    if kind == "no director":
        return {"kind": "no director"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_codex_server(value: object, place: str) -> CodexServer:
    data = _wire_object(value, place)
    return {
        "run_dir": _wire_string(
            _wire_required(data, "run_dir", place), f"{place}.run_dir"
        ),
        "pid": _wire_integer(_wire_required(data, "pid", place), f"{place}.pid"),
        "busy_seats": _wire_strings(
            _wire_required(data, "busy_seats", place), f"{place}.busy_seats"
        ),
    }


def parse_session(value: object, place: str) -> Session:
    """Validate and return one session from an inventory or shutdown record."""
    data = _wire_object(value, place)
    kind = _wire_kind(data, place)
    if kind not in {"showrunner", "unit", "seat", "top-level"}:
        raise InvalidInventory(f"{place}.kind is invalid")
    servers = [
        _parse_codex_server(item, f"{place}.codex_servers[{index}]")
        for index, item in enumerate(
            _wire_list(
                _wire_required(data, "codex_servers", place),
                f"{place}.codex_servers",
            )
        )
    ]
    parsed: dict[str, object] = {
        "kind": kind,
        "session_id": _wire_string(
            _wire_required(data, "session_id", place), f"{place}.session_id"
        ),
        "pid": _wire_integer(_wire_required(data, "pid", place), f"{place}.pid"),
        "proc_start": _wire_string(
            _wire_required(data, "proc_start", place), f"{place}.proc_start"
        ),
        "name": _wire_string(
            _wire_required(data, "name", place), f"{place}.name"
        ),
        "cwd": _wire_string(_wire_required(data, "cwd", place), f"{place}.cwd"),
        "status": _wire_string(
            _wire_required(data, "status", place), f"{place}.status"
        ),
        "model": _parse_model(
            _wire_required(data, "model", place), f"{place}.model"
        ),
        "checkout": _parse_checkout(
            _wire_required(data, "checkout", place), f"{place}.checkout"
        ),
        "run_dirs": _wire_strings(
            _wire_required(data, "run_dirs", place), f"{place}.run_dirs"
        ),
        "codex_servers": servers,
        "timers": _wire_strings(
            _wire_required(data, "timers", place), f"{place}.timers"
        ),
    }
    host = _parse_host(
        _wire_required(data, "host", place), f"{place}.host", kind == "unit"
    )
    if kind == "unit" and host["kind"] != "unit":
        raise InvalidInventory(f"{place}.host.kind is invalid")
    if kind != "unit" and host["kind"] == "unit":
        raise InvalidInventory(f"{place}.host.kind is invalid")
    parsed["host"] = host
    if kind == "showrunner":
        parsed["production"] = _wire_string(
            _wire_required(data, "production", place), f"{place}.production"
        )
        parsed["doc"] = _wire_string(
            _wire_required(data, "doc", place), f"{place}.doc"
        )
    elif kind == "seat":
        parsed["owner"] = _parse_owner(
            _wire_required(data, "owner", place), f"{place}.owner"
        )
    return cast(Session, cast(object, parsed))


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


def _unit_sessions(runners: list[showrunners.Showrunner]) -> dict[str, UnitMarks]:
    found: dict[str, UnitMarks] = {}
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
            found[claude.session_id] = UnitMarks(
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


def _terminal_host(pid: int, desktop: Desktop) -> SessionHost:
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
            if terminal in {"ghostty", "zed"} and child > 0:
                window = {
                    "kind": terminal,
                    "window_shell": child,
                    "desktop": desktop,
                }
                return cast(WindowHost, cast(object, window))
            if terminal == "terminal":
                return {"kind": "terminal"}
            return {"kind": "unknown"}
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
    unit: UnitMarks | None,
    plans: dict[str, str],
    desktops: dict[str, str],
) -> Host:
    session_id = _string(session.get("sessionId"))
    if unit is not None:
        plan = plans.get(session_id)
        return {
            "kind": "unit",
            "production": unit.production,
            "unit": unit.unit,
            "doc": unit.doc,
            "tmux_session": (
                unit.tmux_session
                or _string(session.get("tmux")).partition(":")[0]
            ),
            "plan": (
                {"kind": "plan", "path": plan}
                if plan is not None
                else {"kind": "no run record"}
            ),
        }

    environment = _process_environment(session.get("pid", 0))
    pane = environment.get("TMUX_PANE", "")
    tmux_session = _tmux_session(pane)
    if pane:
        return (
            {"kind": "tmux", "tmux_session": tmux_session}
            if tmux_session
            else {"kind": "unknown"}
        )
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


def _model(session_id: str) -> LastModel:
    project_root = Path.home() / ".claude/projects"
    try:
        matches = list(project_root.rglob(f"{session_id}.jsonl"))
    except OSError:
        return {"kind": "no reply yet"}
    if not matches:
        return {"kind": "no reply yet"}
    try:
        transcript = max(matches, key=lambda path: path.stat().st_mtime)
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"kind": "no reply yet"}
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
    return (
        {"kind": "model", "name": model}
        if model is not None
        else {"kind": "no reply yet"}
    )


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
    unattributed: list[UnattributedSession] = []

    for record in records:
        pid = record.get("pid", 0)
        session_id = _string(record.get("sessionId"))
        name = _string(record.get("name"))
        owner = seat_owners.get(session_id)
        selected = not only or session_id in only or owner in only
        if not selected:
            continue
        if not _record_matches_process(record):
            unattributed.append(
                {"pid": pid, "name": name, "reason": "process start mismatch"}
            )
            continue
        attributed = _configuration_for_session(pid)
        if attributed is None:
            unattributed.append(
                {"pid": pid, "name": name, "reason": "account unreadable"}
            )
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
        common: dict[str, object] = {
            "session_id": session_id,
            "pid": pid,
            "proc_start": _string(record.get("procStart")),
            "name": name,
            "cwd": cwd,
            "status": _string(record.get("status"), "unknown"),
            "model": _model(session_id),
            "checkout": _checkout(cwd),
            "run_dirs": [str(directory) for directory in directories],
            "codex_servers": _codex_servers(directories),
            "timers": _timers(session_id),
        }
        if kind == "showrunner":
            assert production is not None
            value = {
                **common,
                "kind": "showrunner",
                "host": _host(record, None, plans, desktops),
                "production": production.slug,
                "doc": production.doc,
            }
        elif kind == "unit":
            assert unit is not None
            value = {
                **common,
                "kind": "unit",
                "host": _host(record, unit, plans, desktops),
            }
        elif kind == "seat":
            value = {
                **common,
                "kind": "seat",
                "host": _host(record, None, plans, desktops),
                "owner": (
                    {"kind": "director", "session_id": owner}
                    if owner is not None
                    else {"kind": "no director"}
                ),
            }
        else:
            value = {
                **common,
                "kind": "top-level",
                "host": _host(record, None, plans, desktops),
            }
        found.append(cast(Session, cast(object, value)))

    return {
        "machine": _machine(),
        "login": login,
        "label": label_for("claude", login),
        "sessions": found,
        "unattributed": unattributed,
    }
