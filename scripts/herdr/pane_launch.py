"""Read and write retained launches without loading a terminal or lifecycle controller."""
from __future__ import annotations

import fcntl
import json
import os
import sys
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, cast, overload
from enum import Enum
from importlib import import_module
from uuid import uuid4

if TYPE_CHECKING:
    from scripts.production.session_labels import StaffLabels

from scripts.message import sessions
from scripts.herdr.place import HerdrPlace
from scripts.lib.probe_session import PROBE_ENV
from scripts.lib import atomic_file, legacy_identity
from scripts.production import staff

# The launch environment a resume must give back. Credentials never enter the record.
KEPT_NAMES = frozenset({"ENABLE_TOOL_SEARCH", PROBE_ENV})
# Claude flags whose value the resume repeats; every other argument stays out of the record.
NAME_FLAGS = frozenset({"-n", "--name"})
MODEL_FLAGS = frozenset({"--model"})


class PaneLaunch(TypedDict):
    kind: NotRequired[Literal["claude", "codex"]]
    pane_id: str
    session_id: str
    config_dir: NotRequired[str]
    cwd: str
    name: str
    model: str
    codex_policy: NotRequired[list[str]]
    plugin_directories: NotRequired[list[str]]
    remote_control: bool
    environment: dict[str, str]
    # False when the launch flags could not be read: a restore refuses it.
    flags_read: bool
    ended: NotRequired[bool]
    written_at: str
    pid: NotRequired[int]
    proc_start: NotRequired[str]
    producer_binding: NotRequired[bool]


class CurrentProcessEvidence(Protocol):
    """The current conversation, process generation, and environment for a producer publication."""

    @property
    def current_identity(self) -> str: ...
    started: str
    environment: dict[str, str]


class NativeProcessReader(Protocol):
    def native_process(self, pid: int, kind: str) -> CurrentProcessEvidence: ...


@dataclass(frozen=True)
class ProducerStartExpectation:
    """The conversation, backend, folder, and account selected before a producer starts."""

    member: staff.StaffId
    session: staff.StaffId
    kind: Literal["claude", "codex"]
    folder: Path
    home: Path

    def expected_launch(self, entry: PaneLaunch) -> PaneLaunch:
        expected = entry.copy()
        expected.update(session_id=self.session, kind=self.kind, cwd=str(self.folder), config_dir=str(self.home))
        return expected


@contextmanager
def producer_start(directory: Path, expected: ProducerStartExpectation) -> Generator[None]:
    """Publish the selected start contract for the Claude hook or the Codex publisher."""
    path = directory / f".producer-start-{expected.member}"
    contract = json.dumps({"member": expected.member, "session": expected.session, "kind": expected.kind,
                           "folder": str(expected.folder), "home": str(expected.home), "attempt": uuid4().hex})
    with _publication_lock(directory):
        atomic_file.write_text_atomic(path, contract)
    try:
        yield
    finally:
        with _publication_lock(directory):
            if path.exists() and path.read_text(encoding="utf-8") == contract:
                path.unlink()


def config_home(entry: Mapping[str, object], *, record: staff.StaffSnapshot | None = None) -> staff.SessionHome | staff.HomeUnknown:
    """Read a staffed session's home, or an unstaffed launch's config folder."""
    if record is None:
        try:
            record = staff.local_snapshot()
        except staff.StaffAbsent:
            record = staff.StaffSnapshot(())
        except staff.StaffUnreadable:
            return staff.HomeUnknown()
    reference = entry.get("session_id", "")
    row = record.by_member_reference(reference) if isinstance(reference, str) else None
    if row is not None:
        return row.home
    directory = entry.get("config_dir", "")
    return staff.SessionHome(Path(directory)) if isinstance(directory, str) and directory else staff.HomeUnknown()


def _record_home(entry: PaneLaunch) -> PaneLaunch:
    """Publish the pane's home through the staff writer when membership exists."""
    try:
        row = staff.initial_snapshot().by_member_reference(entry["session_id"])
        if row is None:
            return entry
        current = entry.copy()
        directory = current.pop("config_dir", "")
        if directory:
            home = staff.SessionHome(Path(directory).absolute())
            if row.home != home:
                _ = staff.set_home(row.id, home)
        return current
    except (staff.StaffUnreadable, staff.StaffNotFound) as error:
        print(f"anomaly: {entry['session_id']}: pane home is unavailable: {error}", file=sys.stderr)
        return entry


def staff_admitted(row: staff.StaffRow) -> staff.StaffRow:
    """Complete pane publication after the first native identity admission."""
    try:
        publication = newest_publication(record_directory(), (row.current_session,))
        if publication is not None and "config_dir" in publication.entry:
            write(publication.path, _record_home(publication.entry), sources=tuple(publication.path.parent.glob("*.json")))
            return staff.by_id(row.id) or row
    except (OSError, ValueError, staff.StaffNotFound) as error:
        print(f"anomaly: {row.id}: pane publication is unavailable: {error}", file=sys.stderr)
    return row


def record_directory() -> Path:
    return Path(os.environ.get("HERDR_PANE_RECORD_DIR") or Path.home() / ".local/state/herdr-panes")


def read_entries(path: Path) -> dict[str, object]:
    with _publication_lock(path.parent):
        return _read_entries(path)


def _read_entries(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path}: pane records must be an object")
    return cast(dict[str, object], value)


def read(path: Path) -> dict[str, PaneLaunch]:
    try:
        return cast(dict[str, PaneLaunch], read_entries(path))
    except (OSError, ValueError):
        return {}


def _validated_launch(path: Path, pane_id: str, value: object, *, required_strings: frozenset[str]) -> PaneLaunch:
    location = f"{path}: pane {pane_id}"
    if not isinstance(value, dict):
        raise ValueError(f"{location}: launch must be an object")
    launch = cast(dict[str, object], value)
    _ = agent_kind(cast(PaneLaunch, cast(object, launch)))
    for field in ("pane_id", "session_id", "config_dir", "cwd", "name", "model", "written_at"):
        if field not in launch and field not in required_strings:
            continue
        if not isinstance(launch.get(field), str):
            raise ValueError(f"{location}: {field} must be a string")
    for field in ("remote_control", "flags_read", "ended", "producer_binding"):
        if field not in launch:
            continue
        if not isinstance(launch.get(field), bool):
            raise ValueError(f"{location}: {field} must be a boolean")
    environment = launch.get("environment")
    if (not isinstance(environment, dict)
            or not all(isinstance(name, str) and isinstance(setting, str)
                       for name, setting in cast(dict[object, object], environment).items())):
        raise ValueError(f"{location}: environment must contain string names and settings")
    if "plugin_directories" in launch:
        directories = launch["plugin_directories"]
        if (not isinstance(directories, list)
                or not all(isinstance(directory, str) and directory
                           for directory in cast(list[object], directories))):
            raise ValueError(f"{location}: plugin_directories must contain nonempty paths")
    if "codex_policy" in launch:
        policy = launch["codex_policy"]
        if not isinstance(policy, list) or not all(isinstance(value, str) for value in cast(list[object], policy)):
            raise ValueError(f"{location}: codex_policy must contain strings")
    return cast(PaneLaunch, cast(object, launch))


def agent_kind(entry: PaneLaunch) -> Literal["claude", "codex"]:
    """Read the backend kind; records from before this field describe Claude."""
    kind = entry.get("kind", "claude")
    if kind not in ("claude", "codex"):
        raise ValueError(f"invalid retained agent kind: {kind!r}")
    return kind


def read_strict(path: Path) -> dict[str, PaneLaunch]:
    """Require identity evidence; refuse damage in any present launch field."""
    required_strings = frozenset({"session_id", "written_at"})
    return {pane: _validated_launch(path, pane, value, required_strings=required_strings)
            for pane, value in read_entries(path).items()}


def read_valid_entries(path: Path, *, record: staff.StaffSnapshot | None = None) -> dict[str, PaneLaunch]:
    """Keep usable evidence for a readout without losing a damaged entry's siblings."""
    valid: dict[str, PaneLaunch] = {}
    required_strings = frozenset({"session_id", "written_at"})
    if record is None:
        try:
            record = staff.local_snapshot()
        except staff.StaffUnreadable:
            record = staff.StaffSnapshot(())
    for pane, value in read_entries(path).items():
        try:
            entry = _validated_launch(path, pane, value, required_strings=required_strings)
            if "config_dir" in entry or record.by_member_reference(entry["session_id"]) is not None:
                valid[pane] = entry
        except ValueError:
            continue
    return valid


def matching_launch(entries: Mapping[str, PaneLaunch], place: HerdrPlace, session_id: str) -> PaneLaunch | None:
    entry = entries.get(place.pane)
    if entry is None or entry.get("pane_id") != place.pane or entry.get("session_id") != session_id:
        return None
    return entry


class StaffLabelsRead(Enum):
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


class RecordedLabels:
    """Resolve process labels from the staff record without pane records."""

    def __init__(self, membership: StaffLabels | StaffLabelsRead = StaffLabelsRead.PENDING) -> None:
        self.membership: StaffLabels | StaffLabelsRead = membership

    def resolve(self, pid: int, environment: Mapping[str, str]) -> dict[str, str]:
        values = legacy_identity.without_legacy_identity(environment)
        record = sessions.read_session(sessions.sessions_dir() / f"{pid}.json")
        if (isinstance(record, sessions.UnreadableSessionRecord) or record["pid"] != pid
                or not sessions.running_session(record)):
            return values
        if self.membership is StaffLabelsRead.PENDING:
            from scripts.production.session_labels import StaffLabels

            try:
                self.membership = StaffLabels.read()
            except (staff.StaffUnreadable, OSError, ValueError):
                self.membership = StaffLabelsRead.UNAVAILABLE
        if not isinstance(self.membership, StaffLabelsRead):
            values = self.membership.environment(values, record["sessionId"])
        return values


@contextmanager
def locked(path: Path) -> Generator[None]:
    """Hold the lock for one retained launch file."""
    with _publication_lock(path.parent):
        with open(path.parent / f".{path.stem}.lock", "w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


_publication_directories: ContextVar[frozenset[Path]] = ContextVar("publication_directories", default=frozenset())


@contextmanager
def _publication_lock(directory: Path) -> Generator[None]:
    """Serialize record access and complete an interrupted producer publication before access."""
    directory = directory.absolute()
    held = _publication_directories.get()
    if directory in held:
        yield
        return
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".publication.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        scope = _publication_directories.set(held | {directory})
        try:
            journal = directory / ".producer-publication"
            if journal.exists():
                pending = cast(dict[str, dict[str, PaneLaunch]], json.loads(journal.read_text(encoding="utf-8")))
                for filename, entries in pending.items():
                    if Path(filename).name != filename or not filename.endswith(".json"):
                        raise ValueError("invalid producer publication recovery path")
                    atomic_file.write_text_atomic(directory / filename, json.dumps(entries, indent=2, sort_keys=True) + "\n")
                journal.unlink()
            yield
        finally:
            _publication_directories.reset(scope)


def without_staff_labels(entry: PaneLaunch) -> PaneLaunch:
    """Keep pane observations without saved staff labels."""
    current = entry.copy()
    current["environment"] = {key: value for key, value in
        legacy_identity.without_legacy_identity(entry["environment"]).items()
        if key not in {"CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"}}
    return current


def save(path: Path, entries: dict[str, PaneLaunch]) -> None:
    """Publish one retained launch file through the atomic writer."""
    with _publication_lock(path.parent):
        atomic_file.write_text_atomic(path, json.dumps(entries, indent=2, sort_keys=True) + "\n")


def write(path: Path, launch: PaneLaunch, *, sources: Sequence[Path] = ()) -> None:
    """Persist pane observations with the fixed staff reference."""
    try:
        membership = staff.initial_snapshot()
        owner = membership.by_member_reference(launch["session_id"])
    except (staff.StaffUnreadable, OSError, ValueError):
        membership = staff.StaffSnapshot(())
        owner = None
    if owner is not None:
        launch = launch.copy()
        launch["session_id"] = owner.id
        _ = launch.pop("config_dir", None)
    with locked(path):
        if owner is not None and owner.kind == "producer":
            _publish_producer(path, launch, owner, membership, sources)
            return
        entries = read(path)
        if entries.get(launch["pane_id"]) == launch:
            return
        entries[launch["pane_id"]] = launch
        save(path, entries)


def _publish_producer(path: Path, launch: PaneLaunch, owner: staff.StaffRow,
                      membership: staff.StaffSnapshot, sources: Sequence[Path]) -> None:
    """Commit one proved producer binding and remove its other bindings without closing panes."""
    paths = sorted({path.absolute(), *(source.absolute() for source in sources)})
    if any(source.parent != path.absolute().parent for source in paths):
        raise ValueError("producer publication paths must share the record directory")
    records = {source: read_strict(source) for source in paths}
    previous = [entry for entries in records.values() for entry in entries.values()
                if membership.by_member_reference(entry["session_id"]) == owner]
    newest = max(previous, key=lambda entry: entry["written_at"], default=None)
    if newest is not None and newest.get("producer_binding", False):
        if newest == launch or launch["written_at"] < newest["written_at"]:
            return
        if (newest["pane_id"] != launch["pane_id"] and launch["written_at"] == newest["written_at"]):
            raise ValueError("producer publication is older than its current binding")
    pid, generation = launch.get("pid"), launch.get("proc_start", "")
    if not isinstance(pid, int) or isinstance(pid, bool) or not generation:
        if previous:
            current = records[path.absolute()].get(launch["pane_id"])
            if current is None or current.get("producer_binding", False):
                return
            if membership.by_member_reference(current["session_id"]) != owner:
                return
        records[path.absolute()][launch["pane_id"]] = launch
        save(path, records[path.absolute()])
        return
    from scripts.lib import models_registry

    process_reader = cast(NativeProcessReader, cast(object, import_module("scripts.herdr.herdr")))
    observed = process_reader.native_process(pid, agent_kind(launch))
    if (observed.current_identity != owner.current_session or observed.started != generation
            or observed.environment.get("HERDR_PANE_ID") != launch["pane_id"]
            or observed.environment.get("HERDR_SESSION", "default") != path.stem):
        raise ValueError("producer publication does not match the current process")
    expected = launch.copy()
    expected["session_id"] = owner.id
    expected["kind"] = models_registry.launch_settings("producers", owner.name).kind
    contract_path = path.parent / f".producer-start-{owner.id}"
    if contract_path.exists():
        contract = cast(dict[str, str], json.loads(contract_path.read_text(encoding="utf-8")))
        if contract["member"] != owner.id or contract["session"] != owner.current_session:
            raise ValueError("producer start expectation names another conversation")
        expected["cwd"] = contract["folder"]
        if contract["kind"] != agent_kind(launch):
            raise ValueError("producer publication uses another backend")
        variable = "CODEX_HOME" if agent_kind(launch) == "codex" else "CLAUDE_CONFIG_DIR"
        process_home = Path(observed.environment.get(variable) or Path.home() / (".codex" if agent_kind(launch) == "codex" else ".claude"))
        if process_home.resolve() != Path(contract["home"]).resolve():
            raise ValueError("producer publication uses another account")
    elif isinstance(owner.worktree, staff.PresentWorktree):
        expected["cwd"] = str(owner.worktree.path)
    elif newest is not None:
        expected["cwd"] = newest["cwd"]
    _usable_replacement(path, ResumeAttempt(launch["pane_id"], launch), launch,
                        VerifiedResume(expected, frozenset({owner.id, owner.current_session})), same_pane=False)
    current = process_reader.native_process(pid, agent_kind(launch))
    if current.current_identity != observed.current_identity or current.started != generation:
        raise ValueError("producer process changed before publication")
    committed = launch.copy()
    committed["producer_binding"] = True
    updates = {source: {pane: entry for pane, entry in entries.items()
                         if membership.by_member_reference(entry["session_id"]) != owner}
               for source, entries in records.items()}
    updates[path.absolute()][launch["pane_id"]] = committed
    changed = {source: entries for source, entries in updates.items() if entries != records[source]}
    if len(changed) > 1:
        journal = path.parent / ".producer-publication"
        atomic_file.write_text_atomic(journal, json.dumps({source.name: entries for source, entries in changed.items()}))
        for source, entries in changed.items():
            save(source, entries)
        journal.unlink()
    elif changed:
        source, entries = next(iter(changed.items()))
        save(source, entries)


@dataclass(frozen=True)
class LaunchPublication:
    """The source file and exact retained launch before a replacement."""

    path: Path
    entry: PaneLaunch


def newest_publication(directory: Path, references: Sequence[str]) -> LaunchPublication | None:
    """Read the newest retained publication for the supplied session references."""
    matches = [LaunchPublication(path, entry)
               for path in sorted(directory.glob("*.json"))
               for entry in read_strict(path).values()
               if entry["session_id"] in references]
    return max(matches, key=lambda publication: publication.entry["written_at"], default=None)


@dataclass(frozen=True)
class UnpublishedResume:
    """The resume attempt has no retained launch record."""


UNPUBLISHED_RESUME = UnpublishedResume()


@dataclass(frozen=True)
class ResumeAttempt:
    """The attempt's pane and its exact publication before completion."""

    pane_id: str
    publication: PaneLaunch | UnpublishedResume


@dataclass(frozen=True)
class VerifiedResume:
    """The caller proved the expected conversation in the attempt's pane."""

    expected: PaneLaunch
    references: frozenset[str]


@dataclass(frozen=True)
class FailedResume:
    """The attempt did not prove the expected conversation."""


@dataclass(frozen=True)
class PreparedAccountMove:
    """The target account has folder trust and must survive a failed start."""

    config_dir: Path
    account_environment: Mapping[str, str]


class ResumeRecord(Enum):
    SOURCE = "source record"


def resume_attempt(path: Path, pane_id: str) -> ResumeAttempt:
    """Capture the exact publication before identity checks or cleanup."""
    entry = read_strict(path).get(pane_id)
    return ResumeAttempt(pane_id, entry if entry is not None else UnpublishedResume())


def _usable_replacement(path: Path, attempt: ResumeAttempt, entry: PaneLaunch,
                        proof: VerifiedResume, *, same_pane: bool) -> None:
    required = frozenset({"pane_id", "session_id", "cwd", "name", "model", "written_at"})
    _ = _validated_launch(path, attempt.pane_id, entry, required_strings=required)
    expected = proof.expected
    home, expected_home = config_home(entry), config_home(expected)
    home_changed = False
    if isinstance(home, staff.SessionHome) and isinstance(expected_home, staff.SessionHome):
        home_changed = home.path.resolve() != expected_home.path.resolve()
    else:
        print(f"anomaly: {attempt.pane_id}: session home is unknown; keep the selected account", file=sys.stderr)
    if (entry["pane_id"] != attempt.pane_id or entry["session_id"] not in proof.references
            or agent_kind(entry) != agent_kind(expected)
            or home_changed
            or Path(entry["cwd"]).resolve() != Path(expected["cwd"]).resolve()
            or not entry.get("flags_read", False)
            or (not same_pane and entry.get("ended", False))):
        raise ValueError(f"{attempt.pane_id}: replacement launch does not match the proved resume")


def retire_launch(path: Path, pane_id: str, original: PaneLaunch, *,
                  replacement: ResumeAttempt | UnpublishedResume = UNPUBLISHED_RESUME) -> None:
    """Remove the exact old publication after confirmed closure."""
    with locked(path):
        entries = read_strict(path)
        current = entries.get(pane_id)
        if current is None:
            return
        if current != original:
            raise ValueError(f"{pane_id}: retained launch changed before retirement")
        if (isinstance(replacement, ResumeAttempt) and not isinstance(replacement.publication, UnpublishedResume)
                and entries.get(replacement.pane_id) != replacement.publication):
            raise ValueError(f"{replacement.pane_id}: replacement launch changed before retirement")
        del entries[pane_id]
        save(path, entries)


@overload
def finish_restore(path: Path, original: PaneLaunch, attempt: ResumeAttempt,
                   outcome: PreparedAccountMove, *, close: Callable[[str], bool],
                   destination: Path | ResumeRecord = ResumeRecord.SOURCE,
                   close_source: Callable[[str], bool] | ResumeRecord = ResumeRecord.SOURCE) -> LaunchPublication: ...


@overload
def finish_restore(path: Path, original: PaneLaunch, attempt: ResumeAttempt,
                   outcome: VerifiedResume | FailedResume, *, close: Callable[[str], bool],
                   destination: Path | ResumeRecord = ResumeRecord.SOURCE,
                   close_source: Callable[[str], bool] | ResumeRecord = ResumeRecord.SOURCE) -> None: ...


def finish_restore(path: Path, original: PaneLaunch, attempt: ResumeAttempt,
                   outcome: VerifiedResume | FailedResume | PreparedAccountMove, *,
                   close: Callable[[str], bool],
                   destination: Path | ResumeRecord = ResumeRecord.SOURCE,
                   close_source: Callable[[str], bool] | ResumeRecord = ResumeRecord.SOURCE) -> LaunchPublication | None:
    """Keep one proved launch, or retire a failed attempt after confirmed closure.

    The close callback must confirm that the selected tab is closed.
    A same-pane resume keeps its pane on success and failure.
    """
    destination_path = destination if isinstance(destination, Path) else path
    same_pane = path == destination_path and original["pane_id"] == attempt.pane_id
    if isinstance(outcome, PreparedAccountMove):
        if not same_pane:
            raise ValueError("an account move requires its original retained pane")
        with locked(path):
            entries = read_strict(path)
            if entries.get(original["pane_id"]) != original or attempt.publication != original:
                raise ValueError(f"{original['pane_id']}: retained launch changed before the account move")
            prepared = original.copy()
            if "config_dir" in original:
                prepared["config_dir"] = str(outcome.config_dir)
                prepared["environment"] = {**original["environment"], **outcome.account_environment}
            if prepared != original:
                entries[original["pane_id"]] = prepared
                save(path, entries)
            return LaunchPublication(path, prepared)
    if isinstance(outcome, FailedResume):
        if same_pane:
            return
        committed = read(destination_path).get(attempt.pane_id)
        if committed is not None and committed.get("producer_binding", False):
            return
        if close(attempt.pane_id) is not True:
            return
        if not isinstance(attempt.publication, UnpublishedResume):
            retire_launch(destination_path, attempt.pane_id, attempt.publication,
                           replacement=ResumeAttempt(attempt.pane_id, UnpublishedResume()))
        return
    try:
        with locked(destination_path):
            entries = read_strict(destination_path)
            replacement = entries.get(attempt.pane_id)
            if replacement is None:
                raise ValueError(f"{attempt.pane_id}: replacement launch was not published; would delete the only resume record")
            _usable_replacement(destination_path, attempt, replacement, outcome, same_pane=same_pane)
            if same_pane:
                if replacement.get("ended", False):
                    replacement["ended"] = False
                    save(destination_path, entries)
                return
            source = read_strict(path).get(original["pane_id"])
            if source is None and replacement.get("producer_binding", False):
                retained = ResumeAttempt(attempt.pane_id, replacement.copy())
            elif source != original:
                raise ValueError(f"{original['pane_id']}: retained launch changed before replacement")
            else:
                retained = ResumeAttempt(attempt.pane_id, replacement.copy())
    except (OSError, ValueError):
        committed = read(destination_path).get(attempt.pane_id)
        if committed is None or not committed.get("producer_binding", False):
            finish_restore(path, original, attempt, FailedResume(), close=close, destination=destination_path)
        raise
    source_close = close if isinstance(close_source, ResumeRecord) else close_source
    if source_close(original["pane_id"]) is not True:
        return
    if destination_path == path:
        retire_launch(path, original["pane_id"], original, replacement=retained)
    else:
        retire_launch(path, original["pane_id"], original)


@dataclass(frozen=True)
class ClaudeArguments:
    values: dict[str, str]
    has_prompt: bool
    plugin_directories: tuple[str, ...] = ()

    def value(self, flags: frozenset[str]) -> str:
        return next((value for name, value in reversed(tuple(self.values.items())) if name in flags), "")


# Claude options used by launch commands; all consumers share this parse.
CLAUDE_VALUE_FLAGS = frozenset({"--resume", "-r", "--remote-control", "--settings",
    "--model", "--name", "-n", "--permission-mode", "--session-id", "--plugin-dir"})
RESUME_FLAGS = frozenset({"--resume", "-r"})


def parse_claude_arguments(arguments: Sequence[str]) -> ClaudeArguments:
    values: dict[str, str] = {}
    prompt = False
    plugin_directories: list[str] = []
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument == "--":
            prompt = prompt or index < len(arguments)
            break
        option, equals, value = argument.partition("=")
        if option in CLAUDE_VALUE_FLAGS:
            if not equals and index < len(arguments) and not arguments[index].startswith("-"):
                value = arguments[index]
                index += 1
            _ = values.pop(option, None)
            values[option] = value
            if option == "--plugin-dir" and value:
                plugin_directories.append(value)
        elif not argument.startswith("-"):
            prompt = True
    return ClaudeArguments(values, prompt, tuple(plugin_directories))


def launch(pane_id: str, session_id: str, cwd: str, environment: Mapping[str, str],
           arguments: Sequence[str], *, kind: Literal["claude", "codex"] = "claude") -> PaneLaunch:
    config_dir = (environment.get("CODEX_HOME") or str(Path.home() / ".codex") if kind == "codex"
                  else environment.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))
    parsed = parse_claude_arguments(arguments)
    entry: PaneLaunch = {
        "kind": kind,
        "pane_id": pane_id,
        "session_id": session_id,
        "config_dir": config_dir,
        "cwd": cwd,
        "name": parsed.value(NAME_FLAGS),
        "model": parsed.value(MODEL_FLAGS),
        "remote_control": any(argument.partition("=")[0] == "--remote-control" for argument in arguments),
        "environment": {key: value for key, value in sorted(environment.items())
                        if key in KEPT_NAMES},
        "flags_read": bool(arguments),
        "ended": False,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    if kind == "codex":
        policy: list[str] = []
        options = iter(arguments[1:])
        for argument in options:
            if argument == "--":
                break
            option, equals, value = argument.partition("=")
            if option in {"--sandbox", "--ask-for-approval", "-c"}:
                value = value if equals else next(options, "")
                if option != "-c" or not value.startswith("projects="):
                    policy.extend((option, value))
        entry["codex_policy"] = policy
    return _record_home(entry)
