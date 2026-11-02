"""The unit's untracked configuration and its retained retirement facts."""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, replace
from datetime import date as CalendarDate
from pathlib import Path
from typing import cast

from scripts.lib import atomic_file, git_exclude
from scripts.production.staff import StaffId


class ConfigRefused(ValueError):
    """Configuration evidence is incomplete or conflicts with a retained file."""


@dataclass(frozen=True)
class PlanFile:
    path: Path

    def __post_init__(self) -> None:
        if self.path.is_absolute() or ".." in self.path.parts or self.path == Path("."):
            raise ConfigRefused(f"plan: expected a repository-relative file, got {self.path}")
        if self.path.as_posix() in ("standby", "permanent"):
            raise ConfigRefused(f"plan: {self.path} names a plan state, not a file")


@dataclass(frozen=True)
class StandbyPlan:
    pass


@dataclass(frozen=True)
class PermanentPlan:
    pass


@dataclass(frozen=True)
class AppPort:
    number: int

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or not 1 <= self.number <= 65535:
            raise ConfigRefused(f"port: invalid TCP port {self.number!r}")


@dataclass(frozen=True)
class NoAppPort:
    pass


@dataclass(frozen=True)
class RecordedStartFolder:
    path: Path

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ConfigRefused(f"retired.folder: expected an absolute path, got {self.path}")


@dataclass(frozen=True)
class UnrecordedStartFolder:
    pass


@dataclass(frozen=True)
class Branch:
    value: str

    def __post_init__(self) -> None:
        if not self.value or any(character.isspace() for character in self.value):
            raise ConfigRefused(f"retired.branch: invalid branch {self.value!r}")


@dataclass(frozen=True)
class RetiredSession:
    date: str
    session_id: str
    start_folder: RecordedStartFolder | UnrecordedStartFolder
    branch: Branch

    def __post_init__(self) -> None:
        try:
            _ = CalendarDate.fromisoformat(self.date)
        except ValueError as error:
            raise ConfigRefused(f"retired.date: invalid date {self.date!r}") from error
        if not self.session_id.strip():
            raise ConfigRefused("retired.session: missing conversation id")


@dataclass(frozen=True)
class NoRetiredSession:
    """The worktree configuration has no retained retirement facts."""


@dataclass(frozen=True)
class UnitConfig:
    plan: PlanFile | StandbyPlan | PermanentPlan
    port: AppPort | NoAppPort
    responsibilities: tuple[str, ...]
    retired: RetiredSession | NoRetiredSession = field(default_factory=NoRetiredSession)

    def __post_init__(self) -> None:
        for name in self.responsibilities:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None:
                raise ConfigRefused(f"responsibilities: invalid short name {name!r}")


@dataclass(frozen=True)
class ConfigMissing:
    reason: str


@dataclass(frozen=True)
class InWorktree:
    path: Path


@dataclass(frozen=True)
class Parked:
    path: Path


@dataclass(frozen=True)
class Both:
    worktree: Path
    parked: Path
    same: bool


@dataclass(frozen=True)
class Nowhere:
    pass


def parked_path(unit: StaffId) -> Path:
    """Park on this machine, independently of the staff record's owner."""
    if not unit or Path(unit).name != unit or unit in (".", ".."):
        raise ConfigRefused(f"invalid staff id for parked configuration: {unit!r}")
    return Path.home() / ".local/state/staff/units" / f"{unit}.toml"


def _string(fields: dict[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise ConfigRefused(f"{key}: expected a string")
    return value


def _decode(fields: dict[str, object]) -> UnitConfig:
    unknown = set(fields) - {"plan", "port", "responsibilities", "retired"}
    if unknown:
        raise ConfigRefused(f"unknown key: {sorted(unknown)[0]}")
    plan_text = _string(fields, "plan")
    plan = StandbyPlan() if plan_text == "standby" else (
        PermanentPlan() if plan_text == "permanent" else PlanFile(Path(plan_text)))
    port: AppPort | NoAppPort = NoAppPort()
    if "port" in fields:
        number = fields["port"]
        if not isinstance(number, int) or isinstance(number, bool):
            raise ConfigRefused("port: expected an integer")
        port = AppPort(number)
    raw_duties = fields.get("responsibilities")
    if not isinstance(raw_duties, list):
        raise ConfigRefused("responsibilities: expected an array of short names")
    duties: list[str] = []
    for duty in cast(list[object], raw_duties):
        if not isinstance(duty, str):
            raise ConfigRefused("responsibilities: expected an array of short names")
        duties.append(duty)
    retired: RetiredSession | NoRetiredSession = NoRetiredSession()
    if "retired" in fields:
        raw_retired = fields["retired"]
        if not isinstance(raw_retired, dict):
            raise ConfigRefused("retired: expected a table")
        facts = cast(dict[str, object], raw_retired)
        if set(facts) != {"date", "session", "folder", "branch"}:
            raise ConfigRefused("retired: expected date, session, folder and branch")
        folder = _string(facts, "folder")
        retired = RetiredSession(_string(facts, "date"), _string(facts, "session"),
                                 RecordedStartFolder(Path(folder)) if folder else UnrecordedStartFolder(),
                                 Branch(_string(facts, "branch")))
    return UnitConfig(plan, port, tuple(duties), retired)


def _read_file(path: Path, *, cleanup: bool = True) -> UnitConfig | ConfigMissing:
    try:
        if cleanup and path.parent.exists():
            atomic_file.remove_dead_temporary_files(path)
        return _decode(cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8"))))
    except FileNotFoundError:
        return ConfigMissing(f"{path}: configuration is missing")
    except (OSError, UnicodeError, ValueError) as error:
        raise ConfigRefused(f"{path}: {error}") from error


def read(worktree: Path, *, cleanup: bool = True) -> UnitConfig | ConfigMissing:
    """Read configuration; previews retain abandoned recovery files unchanged."""
    return _read_file(worktree / ".unit.toml", cleanup=cleanup)


def read_parked(unit: StaffId, *, cleanup: bool = True) -> UnitConfig | ConfigMissing:
    """Read retained facts on this machine; previews perform no recovery cleanup."""
    return _read_file(parked_path(unit), cleanup=cleanup)


def _fields(config: UnitConfig) -> dict[str, object]:
    if isinstance(config.plan, PlanFile):
        plan = config.plan.path.as_posix()
    else:
        plan = "standby" if isinstance(config.plan, StandbyPlan) else "permanent"
    fields: dict[str, object] = {"plan": plan}
    if isinstance(config.port, AppPort):
        fields["port"] = config.port.number
    fields["responsibilities"] = list(config.responsibilities)
    if isinstance(config.retired, RetiredSession):
        facts = config.retired
        fields["retired"] = {"date": facts.date, "session": facts.session_id,
                             "folder": str(facts.start_folder.path)
                             if isinstance(facts.start_folder, RecordedStartFolder) else "",
                             "branch": facts.branch.value}
    return fields


def _text(config: UnitConfig) -> str:
    fields = _fields(config)
    lines = [f"{key} = {json.dumps(value, ensure_ascii=False)}"
             for key, value in fields.items() if key != "retired"]
    if isinstance(config.retired, RetiredSession):
        lines.extend(("", "[retired]"))
        facts = cast(dict[str, object], fields["retired"])
        lines.extend(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in facts.items())
    return "\n".join(lines) + "\n"


def _publish(path: Path, config: UnitConfig) -> None:
    current = _read_file(path)
    if current != config:
        atomic_file.write_text_atomic(path, _text(config))
    if _read_file(path) != config:
        raise ConfigRefused(f"{path}: configuration did not match after publication")


def write(worktree: Path, config: UnitConfig) -> None:
    """Publish only after git proves the file is untracked and ignored."""
    if not isinstance(config.retired, NoRetiredSession):
        raise ConfigRefused(f"{worktree / '.unit.toml'}: retirement facts belong in the parked file")
    git_exclude.ensure_excluded(worktree, "/.unit.toml")
    _publish(worktree / ".unit.toml", config)


def first_difference(left: UnitConfig, right: UnitConfig) -> str:
    for key in ("plan", "port", "responsibilities"):
        if getattr(left, key) != getattr(right, key):
            return key
    if left.retired != right.retired:
        return "retired"
    return ""


def _check_pair(worktree_path: Path, parked: Path, source: UnitConfig, saved: UnitConfig) -> None:
    key = first_difference(replace(source, retired=NoRetiredSession()),
                            replace(saved, retired=NoRetiredSession()))
    if key:
        raise ConfigRefused(f"{worktree_path} and {parked}: {key} differs; no file was changed")


def place(unit: StaffId, worktree: Path, *, cleanup: bool = True) -> InWorktree | Parked | Both | Nowhere:
    live_path, saved_path = worktree / ".unit.toml", parked_path(unit)
    live, saved = _read_file(live_path, cleanup=cleanup), read_parked(unit, cleanup=cleanup)
    if isinstance(live, UnitConfig) and isinstance(saved, UnitConfig):
        same = not first_difference(replace(live, retired=NoRetiredSession()),
                                     replace(saved, retired=NoRetiredSession()))
        return Both(live_path, saved_path, same)
    if isinstance(live, UnitConfig):
        return InWorktree(live_path)
    if isinstance(saved, UnitConfig):
        return Parked(saved_path)
    return Nowhere()


def write_parked(unit: StaffId, config: UnitConfig) -> None:
    """Publish complete retired facts once; never overwrite different retained facts."""
    if not isinstance(config.retired, RetiredSession):
        raise ConfigRefused(f"{parked_path(unit)}: parked configuration requires retirement facts")
    path = parked_path(unit)
    saved = read_parked(unit)
    if isinstance(saved, UnitConfig):
        key = first_difference(saved, config)
        if key:
            raise ConfigRefused(f"{path}: {key} differs; no file was changed")
    _publish(path, config)


def park(unit: StaffId, worktree: Path, retired: RetiredSession) -> None:
    """Verify the retained copy before retire removes the worktree."""
    source_path, saved_path = worktree / ".unit.toml", parked_path(unit)
    source, saved = _read_file(source_path), _read_file(saved_path)
    if isinstance(source, ConfigMissing):
        if isinstance(saved, UnitConfig) and saved.retired == retired:
            return
        raise ConfigRefused(source.reason)
    desired = replace(source, retired=retired)
    if isinstance(saved, UnitConfig):
        _check_pair(source_path, saved_path, source, saved)
        if saved.retired != retired:
            raise ConfigRefused(f"{source_path} and {saved_path}: retired differs; no file was changed")
    write_parked(unit, desired)


def restore(unit: StaffId, worktree: Path) -> None:
    """Restore the live configuration and keep retirement facts until launch succeeds."""
    live_path, saved_path = worktree / ".unit.toml", parked_path(unit)
    saved, live = _read_file(saved_path), _read_file(live_path)
    if isinstance(saved, ConfigMissing):
        if isinstance(live, UnitConfig):
            git_exclude.require_untracked_ignored(worktree, ".unit.toml")
            return
        raise ConfigRefused(saved.reason)
    if isinstance(live, UnitConfig):
        _check_pair(live_path, saved_path, live, saved)
    write(worktree, replace(saved, retired=NoRetiredSession()))


def discard_parked(unit: StaffId) -> None:
    """Remove the retained facts only after the caller has proved the resumed launch."""
    path = parked_path(unit)
    if path.exists():
        path.unlink()
        atomic_file.sync_folder(path.parent)
