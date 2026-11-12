"""The unit's untracked configuration and its retained retirement facts."""
from __future__ import annotations

import fcntl
import json
import re
import os
import socket
from subprocess import run as run_taplo
import tempfile
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date as CalendarDate
from pathlib import Path
from threading import RLock
from typing import cast

from scripts.lib import atomic_file, conf, git_exclude
from scripts.production.staff import StaffId
from scripts.production import staff


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


def plan_arguments(plan: PlanFile | StandbyPlan | PermanentPlan) -> list[str]:
    """The add_unit launch mode for a plan."""
    match plan:
        case PlanFile(path):
            return ["--plan", str(path)]
        case StandbyPlan():
            return ["--standby"]
        case PermanentPlan():
            return ["--permanent"]


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
class PlanDefault:
    """The request needs the unit's default plan."""


def default_port(worktree: Path, selected: AppPort | NoAppPort) -> AppPort | NoAppPort:
    """Keep an explicit port or choose the lowest available Hana port."""
    if isinstance(selected, AppPort):
        return selected
    manifest = worktree / "Cargo.toml"
    package = cast(dict[str, object], tomllib.loads(manifest.read_text()).get("package", {})) if manifest.is_file() else {}
    if not (worktree / "crates/hana/Cargo.toml").is_file() and package.get("name") != "hana":
        return selected
    settings = conf.read("production")
    first, last = settings.whole("unit_port_first"), settings.whole("unit_port_last")
    reserved = {settings.whole("mac_run_port"), settings.whole("smoke_port")}
    for row in staff.snapshot().rows:
        if row.kind != "unit":
            continue
        configurations = [read_parked(row.id, cleanup=False)]
        if isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
            configurations.append(read(row.worktree.path, cleanup=False))
        reserved.update(config.port.number for config in configurations
                        if isinstance(config, UnitConfig) and isinstance(config.port, AppPort))
    for number in range(first, last + 1):
        if number in reserved:
            continue
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", number))
            except OSError:
                continue
        return AppPort(number)
    raise ConfigRefused(f"port default is unavailable; no free port in {first}..{last}")


def default_plan(worktree: Path, document: Path, unit: str = "") -> PlanFile | StandbyPlan | PermanentPlan:
    """Read a saved configuration or the owner's existing plan record."""
    from scripts.production.production_doc import cell_value, optional_field, FieldPresent

    saved = read(worktree, cleanup=False)
    if isinstance(saved, UnitConfig):
        return saved.plan
    lines = document.read_text().splitlines()
    if unit:
        headers: list[str] = []
        for line in lines:
            if not line.startswith("|"):
                headers = []
                continue
            cells = [cell_value(cell.strip()) for cell in line.strip("|").split("|")]
            if "Unit" in cells and "Plan" in cells:
                headers = cells
            elif headers and len(cells) == len(headers) and cells[headers.index("Unit")] == unit:
                value = cells[headers.index("Plan")]
                if value and value not in ("—", "-"):
                    return StandbyPlan() if value == "standby" else PermanentPlan() if value == "permanent" else PlanFile(Path(value))
    else:
        field = optional_field(lines, "Plan")
        if isinstance(field, FieldPresent):
            return PlanFile(Path(cell_value(field.text)))
        if document.is_relative_to(worktree):
            return PlanFile(document.relative_to(worktree))
    raise ConfigRefused(f"plan default is missing; looked in {worktree / '.unit.toml'} and {document}")


def parse_port_cell(raw: str) -> AppPort | NoAppPort:
    """Read a raw Port cell and keep its text in each refusal."""
    from scripts.production.production_doc import cell_value

    value = cell_value(raw)
    if value.casefold() in ("", "—", "-", "none"):
        return NoAppPort()
    match = re.fullmatch(r"([0-9]+)(?:\s+\([^()]*\))?", value)
    if match is None:
        raise ConfigRefused(f"invalid Port cell {raw!r}")
    try:
        return AppPort(int(match[1]))
    except ValueError as error:
        raise ConfigRefused(f"invalid Port cell {raw!r}: {error}") from error


@dataclass(frozen=True)
class NoFormattingWorktree:
    """The formatter uses its temporary directory."""


@dataclass(frozen=True)
class RetiredSession:
    date: str
    session_id: str

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
class RepositoryTaploConfig:
    text: str


@dataclass(frozen=True)
class DefaultTaploConfig:
    """Use taplo's default formatting options."""


@dataclass(frozen=True)
class Published:
    """The writer replaced the target with the formatted bytes."""


@dataclass(frozen=True)
class AlreadyCurrent:
    """The target already contains the formatted bytes."""


@dataclass(frozen=True)
class UnitConfig:
    plan: PlanFile | StandbyPlan | PermanentPlan
    port: AppPort | NoAppPort
    responsibilities: tuple[str, ...]
    retired: RetiredSession | NoRetiredSession = field(default_factory=NoRetiredSession)
    formatting: RepositoryTaploConfig | DefaultTaploConfig = field(default_factory=DefaultTaploConfig, compare=False)

    def __post_init__(self) -> None:
        # A responsibility is a short name or a repository path.
        # Responsibilities are a set; sorted order matches taplo's reorder_arrays in a repository config.
        object.__setattr__(self, "responsibilities", tuple(sorted(self.responsibilities)))
        for name in self.responsibilities:
            if re.fullmatch(r"[A-Za-z0-9._-][A-Za-z0-9._/-]*", name) is None or ".." in name.split("/"):
                raise ConfigRefused(f"responsibilities: invalid name or repository path {name!r}")


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


_writer_mutex = RLock()
_writer_depth = 0


@contextmanager
def writer_lock() -> Generator[None]:
    """Serialize configuration changes across threads and processes on this machine."""
    global _writer_depth
    with _writer_mutex:
        if _writer_depth:
            _writer_depth += 1
            try:
                yield
            finally:
                _writer_depth -= 1
            return
        path = Path.home() / ".local/state/staff/unit-config.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            _writer_depth = 1
            try:
                yield
            finally:
                _writer_depth = 0
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def parked_path(unit: StaffId, *, home: Path | None = None) -> Path:
    """Park on this machine, independently of the staff record's owner."""
    if not unit or Path(unit).name != unit or unit in (".", ".."):
        raise ConfigRefused(f"invalid staff id for parked configuration: {unit!r}")
    return (Path.home() if home is None else home) / ".local/state/staff/units" / f"{unit}.toml"


def _string(fields: dict[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str):
        raise ConfigRefused(f"{key}: expected a string")
    return value


def decode(fields: dict[str, object]) -> UnitConfig:
    """Validate configuration fields from local or remote TOML."""
    unknown = set(fields) - {"plan", "port", "responsibilities", "retired", "taplo_config"}
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
        if set(facts) != {"date", "session"}:
            raise ConfigRefused("retired: expected date and session")
        retired = RetiredSession(_string(facts, "date"), _string(facts, "session"))
    formatting = RepositoryTaploConfig(_string(fields, "taplo_config")) if "taplo_config" in fields else DefaultTaploConfig()
    return UnitConfig(plan, port, tuple(duties), retired, formatting)


def parse(text: str) -> UnitConfig:
    """Decode configuration text without reading or changing a file."""
    return decode(cast(dict[str, object], tomllib.loads(text)))


def _read_file(path: Path, *, cleanup: bool = True) -> UnitConfig | ConfigMissing:
    try:
        if cleanup and path.parent.exists():
            atomic_file.remove_dead_temporary_files(path)
        return parse(path.read_text(encoding="utf-8"))
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
    if isinstance(config.formatting, RepositoryTaploConfig):
        fields["taplo_config"] = config.formatting.text
    if isinstance(config.retired, RetiredSession):
        facts = config.retired
        fields["retired"] = {"date": facts.date, "session": facts.session_id}
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


def repository_formatting(worktree: Path) -> RepositoryTaploConfig | DefaultTaploConfig:
    """Read the repository's taplo configuration when the caller needs it."""
    for name in (".taplo.toml", "taplo.toml"):
        try:
            return RepositoryTaploConfig((worktree / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
    return DefaultTaploConfig()


def _formatted(config: UnitConfig, worktree: Path | NoFormattingWorktree,
               formatting: RepositoryTaploConfig | DefaultTaploConfig) -> bytes:
    """Run taplo with explicit configuration and a repository-relative input path."""
    try:
        with tempfile.TemporaryDirectory(prefix="unit-taplo-") as directory:
            root = worktree if isinstance(worktree, Path) and worktree.is_dir() else Path(directory)
            options: list[str] = []
            if isinstance(formatting, RepositoryTaploConfig):
                settings = Path(directory) / "taplo.toml"
                for name in (".taplo.toml", "taplo.toml"):
                    candidate = root / name
                    if candidate.is_file() and candidate.read_text(encoding="utf-8") == formatting.text:
                        settings = candidate
                        break
                if settings.parent == Path(directory):
                    root = Path(directory)
                    atomic_file.write_text_atomic(settings, formatting.text)
                options.extend(("--config", str(settings)))
            command = ["taplo", "fmt", "--no-auto-config", *options,
                       "--stdin-filepath", str(root / ".unit.toml"), "-"]
            environment = dict(os.environ)
            _ = environment.pop("TAPLO_CONFIG", None)
            result = run_taplo(command, input=_text(config).encode("utf-8"),
                               capture_output=True, check=False, cwd=root, env=environment)
    except (OSError, UnicodeError) as error:
        raise ConfigRefused(f"{worktree}: cannot format configuration with taplo: {error}") from error
    if result.returncode:
        raise ConfigRefused(f"{worktree}: taplo failed: {result.stderr.decode('utf-8', errors='replace').strip()}")
    if decode(cast(dict[str, object], tomllib.loads(result.stdout.decode("utf-8")))) != config:
        raise ConfigRefused(f"{worktree}: taplo changed configuration values")
    return result.stdout


def _differs(path: Path, text: bytes) -> bool:
    try:
        return path.read_bytes() != text
    except FileNotFoundError:
        return True


def _publish(path: Path, config: UnitConfig, text: bytes) -> Published | AlreadyCurrent:
    with writer_lock():
        changed = _differs(path, text)
        if changed:
            atomic_file.write_bytes_atomic(path, text)
        if _differs(path, text) or _read_file(path) != config:
            raise ConfigRefused(f"{path}: configuration did not match after publication")
        return Published() if changed else AlreadyCurrent()


def needs_write(worktree: Path, config: UnitConfig) -> bool:
    """Compare the current bytes with the repository's formatted configuration."""
    desired = replace(config, formatting=DefaultTaploConfig())
    return _differs(worktree / ".unit.toml", _formatted(desired, worktree, repository_formatting(worktree)))


def write(worktree: Path, config: UnitConfig) -> Published | AlreadyCurrent:
    """Publish only after git proves the file is untracked and ignored."""
    with writer_lock():
        if not isinstance(config.retired, NoRetiredSession):
            raise ConfigRefused(f"{worktree / '.unit.toml'}: retirement facts belong in the parked file")
        desired = replace(config, formatting=DefaultTaploConfig())
        formatted = _formatted(desired, worktree, repository_formatting(worktree))
        git_exclude.ensure_excluded(worktree, "/.unit.toml")
        return _publish(worktree / ".unit.toml", desired, formatted)


def set_plan(worktree: Path, plan: Path) -> None:
    """Change only the plan in the existing unit configuration."""
    with writer_lock():
        config = read(worktree, cleanup=False)
        if isinstance(config, ConfigMissing):
            raise staff.StaffUnreadable(config.reason)
        _ = write(worktree, replace(config, plan=PlanFile(plan)))


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


def _parked_config(unit: StaffId, config: UnitConfig) -> UnitConfig:
    row = staff.snapshot().by_id(unit)
    if row is not None and not isinstance(row.worktree, staff.WorktreeUnfilled) and row.worktree.path.is_dir():
        return replace(config, formatting=repository_formatting(row.worktree.path))
    saved = read_parked(unit, cleanup=False)
    formatting = (saved.formatting if isinstance(saved, UnitConfig)
                  and isinstance(saved.formatting, RepositoryTaploConfig) else config.formatting)
    return replace(config, formatting=formatting)


def _parked_text(config: UnitConfig) -> bytes:
    return _formatted(config, NoFormattingWorktree(), config.formatting)


def parked_needs_write(unit: StaffId, config: UnitConfig) -> bool:
    """Compare retained bytes with the recorded repository formatting."""
    return _differs(parked_path(unit), _parked_text(_parked_config(unit, config)))


def write_parked(unit: StaffId, config: UnitConfig) -> Published | AlreadyCurrent:
    """Publish complete retired facts once; never overwrite different retained facts."""
    with writer_lock():
        if not isinstance(config.retired, RetiredSession):
            raise ConfigRefused(f"{parked_path(unit)}: parked configuration requires retirement facts")
        path = parked_path(unit)
        saved = read_parked(unit)
        if isinstance(saved, UnitConfig):
            key = first_difference(saved, config)
            if key:
                raise ConfigRefused(f"{path}: {key} differs; no file was changed")
        desired = _parked_config(unit, config)
        return _publish(path, desired, _parked_text(desired))


def repair_port(target: InWorktree | Parked, recorded_old: AppPort | NoAppPort,
                settled: AppPort | NoAppPort) -> Published | AlreadyCurrent:
    """Change a recorded port only when its previous value still matches the evidence."""
    with writer_lock():
        path = target.path
        source = _read_file(path, cleanup=False)
        if isinstance(source, ConfigMissing):
            raise ConfigRefused(source.reason)
        if source.port == settled:
            return AlreadyCurrent()
        if source.port != recorded_old:
            raise ConfigRefused(f"{path}: port {source.port} differs from recorded old value {recorded_old}; "
                                + f"settled value is {settled}; no file was changed")
        previous = path.read_bytes()
        if parse(previous.decode("utf-8")) != source:
            raise ConfigRefused(f"{path}: configuration changed during port repair; no file was changed")
        desired = replace(source, port=settled)
        if isinstance(target, InWorktree):
            if path.name != ".unit.toml" or not isinstance(source.retired, NoRetiredSession):
                raise ConfigRefused(f"{path}: expected active .unit.toml configuration")
            desired = replace(desired, formatting=DefaultTaploConfig())
            formatted = _formatted(desired, path.parent, repository_formatting(path.parent))
            git_exclude.require_untracked_ignored(path.parent, ".unit.toml")
        else:
            if not isinstance(source.retired, RetiredSession):
                raise ConfigRefused(f"{path}: parked configuration requires retirement facts")
            formatted = _parked_text(desired)
        if path.read_bytes() != previous:
            raise ConfigRefused(f"{path}: configuration changed during port repair; no file was changed")
        return _publish(path, desired, formatted)


def park(unit: StaffId, worktree: Path, retired: RetiredSession) -> Published | AlreadyCurrent:
    """Verify the retained copy before retire removes the worktree."""
    with writer_lock():
        source_path, saved_path = worktree / ".unit.toml", parked_path(unit)
        source, saved = _read_file(source_path), _read_file(saved_path)
        if isinstance(source, ConfigMissing):
            if isinstance(saved, UnitConfig) and saved.retired == retired:
                return write_parked(unit, saved)
            raise ConfigRefused(source.reason)
        desired = replace(source, retired=retired, formatting=repository_formatting(worktree))
        if isinstance(saved, UnitConfig):
            _check_pair(source_path, saved_path, source, saved)
            if saved.retired != retired:
                raise ConfigRefused(f"{source_path} and {saved_path}: retired differs; no file was changed")
        return write_parked(unit, desired)


def restore(unit: StaffId, worktree: Path) -> None:
    """Restore the live configuration and keep retirement facts until launch succeeds."""
    with writer_lock():
        live_path, saved_path = worktree / ".unit.toml", parked_path(unit)
        saved, live = _read_file(saved_path), _read_file(live_path)
        if isinstance(saved, ConfigMissing):
            if isinstance(live, UnitConfig):
                git_exclude.require_untracked_ignored(worktree, ".unit.toml")
                return
            raise ConfigRefused(saved.reason)
        if isinstance(live, UnitConfig):
            _check_pair(live_path, saved_path, live, saved)
        _ = write(worktree, replace(saved, retired=NoRetiredSession()))


def discard_parked(unit: StaffId) -> None:
    """Remove the retained facts only after the caller has proved the resumed launch."""
    with writer_lock():
        path = parked_path(unit)
        if path.exists():
            path.unlink()
            atomic_file.sync_folder(path.parent)
