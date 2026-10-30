"""Session and recovery data shared by observation and shutdown storage."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict, cast


class PlanFromRunRecord(TypedDict):
    kind: Literal["plan"]
    path: str


class NoRunRecord(TypedDict):
    kind: Literal["no run record"]


UnitPlan = PlanFromRunRecord | NoRunRecord


class UnitHost(TypedDict):
    kind: Literal["unit"]
    unit: str
    session_name: str
    session_host: Literal["herdr"]
    plan: UnitPlan


class LegacyTmuxUnitHost(TypedDict):
    kind: Literal["legacy tmux unit"]
    unit: str
    session_name: str


class LegacyTmuxHost(TypedDict):
    kind: Literal["legacy tmux"]
    session_name: str


class HerdrHost(TypedDict):
    kind: Literal["herdr"]
    herdr_session: str
    pane_id: str
    presentation: NotRequired[Literal["attached window", "detached"]]


class NamedDesktop(TypedDict):
    kind: Literal["named"]
    name: str


class DesktopNotInSnapshot(TypedDict):
    kind: Literal["not in snapshot"]


Desktop = NamedDesktop | DesktopNotInSnapshot


class SupervisorIdentity(TypedDict):
    kind: Literal["supervisor"]
    pid: int
    proc_start: str


class NoSupervisor(TypedDict):
    kind: Literal["no supervisor"]


class WindowHost(TypedDict):
    kind: Literal["ghostty", "zed"]
    window_shell: int
    desktop: Desktop
    supervisor: NotRequired[SupervisorIdentity | NoSupervisor]


class TerminalHost(TypedDict):
    kind: Literal["terminal"]
    supervisor: NotRequired[SupervisorIdentity | NoSupervisor]


class UnknownHost(TypedDict):
    kind: Literal["unknown"]


Host = UnitHost | LegacyTmuxUnitHost | LegacyTmuxHost | HerdrHost | WindowHost | TerminalHost | UnknownHost


SessionHost = LegacyTmuxHost | HerdrHost | WindowHost | TerminalHost | UnknownHost


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


WorkerOwner = DirectorOwner | NoDirector


class CodexServer(TypedDict):
    """A live Codex app-server owned by one Claude session's run folder."""

    run_dir: str
    pid: int
    busy_workers: list[str]


class SessionFields(TypedDict):
    session_id: str
    pid: int
    proc_start: str
    name: str
    cwd: str
    # Records from before account-directory capture use the default directory.
    config_dir: NotRequired[str]
    probe: bool
    status: str
    updated_at: int
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
    host: UnitHost | LegacyTmuxUnitHost


class WorkerSession(SessionFields):
    kind: Literal["worker"]
    host: SessionHost
    owner: WorkerOwner


class TopLevelSession(SessionFields):
    kind: Literal["top-level"]
    host: SessionHost


Session = ShowrunnerSession | UnitSession | WorkerSession | TopLevelSession


class TimerRestore(TypedDict):
    instance: str
    was_enabled: bool


class CodexSessionId(TypedDict):
    kind: Literal["session id"]
    session_id: str


class CodexResumePicker(TypedDict):
    kind: Literal["picker"]


class CodexLaunch(TypedDict):
    home: str
    resume: CodexSessionId | CodexResumePicker


class RecoveryHostProcess(TypedDict):
    kind: Literal["process"]
    pid: int
    proc_start: str


class RecoveryHostUnrecorded(TypedDict):
    kind: Literal["not recorded"]


class SnapshotClaudeSession(TypedDict):
    agent: Literal["claude"]
    session: Session
    workers: list[Session]
    timers: list[TimerRestore]
    last_seen: str
    boot_id: NotRequired[str]
    crash: NotRequired[Literal["reboot", "host gone", "agent exited"]]
    host_process: NotRequired[RecoveryHostProcess | RecoveryHostUnrecorded]
    state: Literal["running", "unresolved"]


class SnapshotCodexSession(TypedDict):
    agent: Literal["codex"]
    session: TopLevelSession
    codex: CodexLaunch
    workers: list[Session]
    timers: list[TimerRestore]
    last_seen: str
    boot_id: NotRequired[str]
    crash: NotRequired[Literal["reboot", "host gone"]]
    host_process: NotRequired[RecoveryHostProcess | RecoveryHostUnrecorded]
    state: Literal["running", "unresolved"]


SnapshotSession = SnapshotClaudeSession | SnapshotCodexSession



@dataclass(frozen=True)
class KnownBoot:
    boot_id: str


@dataclass(frozen=True)
class BootNotRecorded:
    """An older caller supplied no boot observation."""


@dataclass(frozen=True)
class CompleteInventory:
    machine: str
    sessions: dict[str, list[SnapshotSession]]
    labels: dict[str, str]
    boot: KnownBoot | BootNotRecorded = BootNotRecorded()
    gone_hosts: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class PartialInventory:
    failures: tuple[str, ...]


class InvalidInventory(ValueError):
    """An inventory value does not satisfy its wire contract."""


def wire_object(value: object, place: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidInventory(f"{place} must be an object")
    return cast(dict[str, object], value)


def wire_required(data: dict[str, object], key: str, place: str) -> object:
    if key not in data:
        raise InvalidInventory(f"{place}.{key} is missing")
    return data[key]


def wire_string(value: object, place: str) -> str:
    if not isinstance(value, str):
        raise InvalidInventory(f"{place} must be a string")
    return value


def wire_integer(value: object, place: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidInventory(f"{place} must be an integer")
    return value


def wire_strings(value: object, place: str) -> list[str]:
    if not isinstance(value, list):
        raise InvalidInventory(f"{place} must be a list")
    values = cast(list[object], value)
    if not all(isinstance(item, str) for item in values):
        raise InvalidInventory(f"{place} must contain only strings")
    return cast(list[str], values)


def wire_list(value: object, place: str) -> list[object]:
    if not isinstance(value, list):
        raise InvalidInventory(f"{place} must be a list")
    return cast(list[object], value)


def wire_kind(data: dict[str, object], place: str) -> str:
    return wire_string(wire_required(data, "kind", place), f"{place}.kind")


def _parse_unit_plan(value: object, place: str) -> UnitPlan:
    data = wire_object(value, place)
    kind = wire_kind(data, place)
    if kind == "plan":
        return {
            "kind": "plan",
            "path": wire_string(
                wire_required(data, "path", place), f"{place}.path"
            ),
        }
    if kind == "no run record":
        return {"kind": "no run record"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_desktop(value: object, place: str) -> Desktop:
    data = wire_object(value, place)
    kind = wire_kind(data, place)
    if kind == "named":
        return {
            "kind": "named",
            "name": wire_string(
                wire_required(data, "name", place), f"{place}.name"
            ),
        }
    if kind == "not in snapshot":
        return {"kind": "not in snapshot"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_host(value: object, place: str, allow_unit: bool) -> Host:
    data = wire_object(value, place)
    kind = wire_kind(data, place)
    if kind in {"unit", "legacy tmux unit"} and allow_unit:
        name_key = "tmux_session" if "tmux_session" in data and "session_name" not in data else "session_name"
        session_host = data.get("session_host", "tmux")
        if session_host not in ("tmux", "herdr"):
            raise InvalidInventory(f"{place}.session_host is invalid")
        unit = wire_string(wire_required(data, "unit", place), f"{place}.unit")
        name = wire_string(wire_required(data, name_key, place), f"{place}.{name_key}")
        if session_host == "tmux" or kind == "legacy tmux unit":
            return LegacyTmuxUnitHost(kind="legacy tmux unit", unit=unit, session_name=name)
        return UnitHost(kind="unit", unit=unit,
                        session_name=name, session_host="herdr",
                        plan=_parse_unit_plan(wire_required(data, "plan", place), f"{place}.plan"))
    if kind == "herdr":
        presentation = wire_string(data.get("presentation", "detached"), f"{place}.presentation")
        if presentation not in {"attached window", "detached"}:
            raise InvalidInventory(f"{place}.presentation: expected attached window or detached")
        return {
            "kind": "herdr",
            "herdr_session": wire_string(
                wire_required(data, "herdr_session", place), f"{place}.herdr_session"
            ),
            "pane_id": wire_string(
                wire_required(data, "pane_id", place), f"{place}.pane_id"
            ),
            "presentation": cast(Literal["attached window", "detached"], presentation),
        }
    if kind in {"tmux", "legacy tmux"}:
        key = "tmux_session" if kind == "tmux" else "session_name"
        return LegacyTmuxHost(kind="legacy tmux", session_name=wire_string(
            wire_required(data, key, place), f"{place}.{key}"))
    if kind in {"ghostty", "zed"}:
        window = {
            "kind": kind,
            "window_shell": wire_integer(
                wire_required(data, "window_shell", place),
                f"{place}.window_shell",
            ),
            "desktop": _parse_desktop(
                wire_required(data, "desktop", place), f"{place}.desktop"
            ),
            "supervisor": _parse_supervisor(data.get("supervisor"), f"{place}.supervisor"),
        }
        return cast(WindowHost, cast(object, window))
    if kind == "terminal":
        if "supervisor" in data:
            return {"kind": "terminal", "supervisor": _parse_supervisor(
                data["supervisor"], f"{place}.supervisor")}
        return {"kind": "terminal"}
    if kind == "unknown":
        return {"kind": "unknown"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_supervisor(value: object, place: str) -> SupervisorIdentity | NoSupervisor:
    if value is None:
        return NoSupervisor(kind="no supervisor")
    data = wire_object(value, place)
    kind = wire_string(wire_required(data, "kind", place), f"{place}.kind")
    if kind == "no supervisor":
        return NoSupervisor(kind="no supervisor")
    if kind != "supervisor":
        raise InvalidInventory(f"{place}.kind is invalid")
    pid = wire_integer(wire_required(data, "pid", place), f"{place}.pid")
    start = wire_string(wire_required(data, "proc_start", place), f"{place}.proc_start")
    if pid <= 1 or not start:
        raise InvalidInventory(f"{place} has no process identity")
    return SupervisorIdentity(kind="supervisor", pid=pid, proc_start=start)


def _parse_head(value: object, place: str) -> Head:
    data = wire_object(value, place)
    kind = wire_kind(data, place)
    if kind == "branch":
        return {
            "kind": "branch",
            "name": wire_string(
                wire_required(data, "name", place), f"{place}.name"
            ),
        }
    if kind == "detached":
        return {
            "kind": "detached",
            "commit": wire_string(
                wire_required(data, "commit", place), f"{place}.commit"
            ),
        }
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_upstream(value: object, place: str) -> Upstream:
    data = wire_object(value, place)
    kind = wire_kind(data, place)
    if kind == "tracking":
        return {
            "kind": "tracking",
            "ahead": wire_integer(
                wire_required(data, "ahead", place), f"{place}.ahead"
            ),
        }
    if kind == "no upstream":
        return {"kind": "no upstream"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_checkout(value: object, place: str) -> CheckoutState:
    data = wire_object(value, place)
    kind = wire_kind(data, place)
    if kind == "git":
        return {
            "kind": "git",
            "path": wire_string(
                wire_required(data, "path", place), f"{place}.path"
            ),
            "head": _parse_head(
                wire_required(data, "head", place), f"{place}.head"
            ),
            "upstream": _parse_upstream(
                wire_required(data, "upstream", place), f"{place}.upstream"
            ),
            "dirty": wire_strings(
                wire_required(data, "dirty", place), f"{place}.dirty"
            ),
        }
    if kind == "not a checkout":
        return {"kind": "not a checkout"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_model(value: object, place: str) -> LastModel:
    data = wire_object(value, place)
    kind = wire_kind(data, place)
    if kind == "model":
        return {
            "kind": "model",
            "name": wire_string(
                wire_required(data, "name", place), f"{place}.name"
            ),
        }
    if kind == "no reply yet":
        return {"kind": "no reply yet"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_owner(value: object, place: str) -> WorkerOwner:
    data = wire_object(value, place)
    kind = wire_kind(data, place)
    if kind == "director":
        return {
            "kind": "director",
            "session_id": wire_string(
                wire_required(data, "session_id", place), f"{place}.session_id"
            ),
        }
    if kind == "no director":
        return {"kind": "no director"}
    raise InvalidInventory(f"{place}.kind is invalid")


def _parse_codex_server(value: object, place: str) -> CodexServer:
    data = wire_object(value, place)
    return {
        "run_dir": wire_string(
            wire_required(data, "run_dir", place), f"{place}.run_dir"
        ),
        "pid": wire_integer(wire_required(data, "pid", place), f"{place}.pid"),
        "busy_workers": wire_strings(
            wire_required(data, "busy_workers", place), f"{place}.busy_workers"
        ),
    }


def parse_session(value: object, place: str, *, require_updated_at: bool = False) -> Session:
    """Validate and return one session from an inventory or shutdown record."""
    data = wire_object(value, place)
    kind = wire_kind(data, place)
    if kind not in {"showrunner", "unit", "worker", "top-level"}:
        raise InvalidInventory(f"{place}.kind is invalid")
    servers = [
        _parse_codex_server(item, f"{place}.codex_servers[{index}]")
        for index, item in enumerate(
            wire_list(
                wire_required(data, "codex_servers", place),
                f"{place}.codex_servers",
            )
        )
    ]
    parsed: dict[str, object] = {
        "kind": kind,
        "session_id": wire_string(
            wire_required(data, "session_id", place), f"{place}.session_id"
        ),
        "pid": wire_integer(wire_required(data, "pid", place), f"{place}.pid"),
        "proc_start": wire_string(
            wire_required(data, "proc_start", place), f"{place}.proc_start"
        ),
        "name": wire_string(
            wire_required(data, "name", place), f"{place}.name"
        ),
        "cwd": wire_string(wire_required(data, "cwd", place), f"{place}.cwd"),
        "status": wire_string(
            wire_required(data, "status", place), f"{place}.status"
        ),
        # Old shutdown records predate this field; zero cannot answer a pause.
        "updated_at": wire_integer(
            wire_required(data, "updated_at", place) if require_updated_at else data.get("updated_at", 0),
            f"{place}.updated_at",
        ),
        "model": _parse_model(
            wire_required(data, "model", place), f"{place}.model"
        ),
        "checkout": _parse_checkout(
            wire_required(data, "checkout", place), f"{place}.checkout"
        ),
        "run_dirs": wire_strings(
            wire_required(data, "run_dirs", place), f"{place}.run_dirs"
        ),
        "codex_servers": servers,
        "timers": wire_strings(
            wire_required(data, "timers", place), f"{place}.timers"
        ),
    }
    probe = data.get("probe", False)
    if not isinstance(probe, bool):
        raise InvalidInventory(f"{place}.probe must be a boolean")
    parsed["probe"] = probe
    if "config_dir" in data:
        parsed["config_dir"] = wire_string(
            data["config_dir"], f"{place}.config_dir"
        )
    host = _parse_host(
        wire_required(data, "host", place), f"{place}.host", kind == "unit"
    )
    if kind == "unit" and host["kind"] not in {"unit", "legacy tmux unit"}:
        raise InvalidInventory(f"{place}.host.kind is invalid")
    if kind != "unit" and host["kind"] in {"unit", "legacy tmux unit"}:
        raise InvalidInventory(f"{place}.host.kind is invalid")
    parsed["host"] = host
    if kind == "showrunner":
        parsed["production"] = wire_string(
            wire_required(data, "production", place), f"{place}.production"
        )
        parsed["doc"] = wire_string(
            wire_required(data, "doc", place), f"{place}.doc"
        )
    elif kind == "worker":
        parsed["owner"] = _parse_owner(
            wire_required(data, "owner", place), f"{place}.owner"
        )
    return cast(Session, cast(object, parsed))


