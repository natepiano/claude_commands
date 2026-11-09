"""Read and write retained launches without loading a terminal or lifecycle controller."""
from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast, overload
from enum import Enum

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
    config_dir: str
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


def record_directory() -> Path:
    return Path(os.environ.get("HERDR_PANE_RECORD_DIR") or Path.home() / ".local/state/herdr-panes")


def read_entries(path: Path) -> dict[str, object]:
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
    for field in ("remote_control", "flags_read", "ended"):
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


def read_valid_entries(path: Path) -> dict[str, PaneLaunch]:
    """Keep usable evidence for a readout without losing a damaged entry's siblings."""
    valid: dict[str, PaneLaunch] = {}
    required_strings = frozenset({"session_id", "written_at", "config_dir"})
    for pane, value in read_entries(path).items():
        try:
            valid[pane] = _validated_launch(path, pane, value, required_strings=required_strings)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / f".{path.stem}.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def without_staff_labels(entry: PaneLaunch) -> PaneLaunch:
    """Keep pane observations without saved staff labels."""
    current = entry.copy()
    current["environment"] = {key: value for key, value in
        legacy_identity.without_legacy_identity(entry["environment"]).items()
        if key not in {"CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"}}
    return current


def save(path: Path, entries: dict[str, PaneLaunch]) -> None:
    """Publish one retained launch file through the atomic writer."""
    atomic_file.write_text_atomic(path, json.dumps(entries, indent=2, sort_keys=True) + "\n")


def write(path: Path, launch: PaneLaunch) -> None:
    """Persist pane observations with the fixed staff reference."""
    try:
        owner = staff.initial_snapshot().by_current_session(launch["session_id"])
    except (staff.StaffUnreadable, OSError, ValueError):
        owner = None
    if owner is not None and owner.id != launch["session_id"]:
        launch = launch.copy()
        launch["session_id"] = owner.id
    with locked(path):
        entries = read(path)
        if entries.get(launch["pane_id"]) == launch:
            return
        entries[launch["pane_id"]] = launch
        save(path, entries)


@dataclass(frozen=True)
class LaunchPublication:
    """The source file and exact retained launch before a replacement."""

    path: Path
    entry: PaneLaunch


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


def resume_attempt(path: Path, pane_id: str) -> ResumeAttempt:
    """Capture the exact publication before identity checks or cleanup."""
    entry = read_strict(path).get(pane_id)
    return ResumeAttempt(pane_id, entry if entry is not None else UnpublishedResume())


def _usable_replacement(path: Path, attempt: ResumeAttempt, entry: PaneLaunch,
                        proof: VerifiedResume, *, same_pane: bool) -> None:
    required = frozenset({"pane_id", "session_id", "config_dir", "cwd", "name", "model", "written_at"})
    _ = _validated_launch(path, attempt.pane_id, entry, required_strings=required)
    expected = proof.expected
    if (entry["pane_id"] != attempt.pane_id or entry["session_id"] not in proof.references
            or agent_kind(entry) != agent_kind(expected)
            or Path(entry["config_dir"]).resolve() != Path(expected["config_dir"]).resolve()
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
                   outcome: PreparedAccountMove, *, close: Callable[[str], bool]) -> LaunchPublication: ...


@overload
def finish_restore(path: Path, original: PaneLaunch, attempt: ResumeAttempt,
                   outcome: VerifiedResume | FailedResume, *, close: Callable[[str], bool]) -> None: ...


def finish_restore(path: Path, original: PaneLaunch, attempt: ResumeAttempt,
                   outcome: VerifiedResume | FailedResume | PreparedAccountMove, *,
                   close: Callable[[str], bool]) -> LaunchPublication | None:
    """Keep one proved launch, or retire a failed attempt after confirmed closure.

    The close callback must confirm that the selected tab is closed.
    A same-pane resume keeps its pane on success and failure.
    """
    same_pane = original["pane_id"] == attempt.pane_id
    if isinstance(outcome, PreparedAccountMove):
        if not same_pane:
            raise ValueError("an account move requires its original retained pane")
        with locked(path):
            entries = read_strict(path)
            if entries.get(original["pane_id"]) != original or attempt.publication != original:
                raise ValueError(f"{original['pane_id']}: retained launch changed before the account move")
            prepared = original.copy()
            prepared["config_dir"] = str(outcome.config_dir)
            prepared["environment"] = {**original["environment"], **outcome.account_environment}
            if prepared != original:
                entries[original["pane_id"]] = prepared
                save(path, entries)
            return LaunchPublication(path, prepared)
    if isinstance(outcome, FailedResume):
        if same_pane:
            return
        if close(attempt.pane_id) is not True:
            return
        if not isinstance(attempt.publication, UnpublishedResume):
            retire_launch(path, attempt.pane_id, attempt.publication,
                           replacement=ResumeAttempt(attempt.pane_id, UnpublishedResume()))
        return
    try:
        with locked(path):
            entries = read_strict(path)
            replacement = entries.get(attempt.pane_id)
            if replacement is None:
                raise ValueError(f"{attempt.pane_id}: replacement launch was not published; would delete the only resume record")
            _usable_replacement(path, attempt, replacement, outcome, same_pane=same_pane)
            if same_pane:
                if replacement.get("ended", False):
                    replacement["ended"] = False
                    save(path, entries)
                return
            if entries.get(original["pane_id"]) != original:
                raise ValueError(f"{original['pane_id']}: retained launch changed before replacement")
            retained = ResumeAttempt(attempt.pane_id, replacement.copy())
    except (OSError, ValueError):
        finish_restore(path, original, attempt, FailedResume(), close=close)
        raise
    if close(original["pane_id"]) is not True:
        return
    retire_launch(path, original["pane_id"], original, replacement=retained)


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
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
    return entry
