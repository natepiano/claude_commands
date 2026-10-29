#!/usr/bin/env python3
"""Build and inspect Claude and Codex account directories; select Claude roles."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
import fcntl
import json
import math
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import override, TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, cast, overload
from importlib import import_module


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"
# Direct CLI and imported callers share the picker's domain variants.
_ = sys.modules.setdefault("scripts.accounts.accounts", sys.modules[__name__])
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(SCRIPTS / "whoami"))

import account  # noqa: E402
from account import ResolvedHistoryLabel as ResolvedHistoryLabel, HistoricalAccountUnresolved as HistoricalAccountUnresolved  # noqa: E402
from scripts.lib import unit_runs  # noqa: E402
from scripts.lib.probe_session import PROBE_ENV  # noqa: E402
import agent_accounts  # noqa: E402
import note_file  # noqa: E402
import usage_store  # noqa: E402
from scripts.accounts import refusals  # noqa: E402
from scripts.accounts import moves, worker_names, role_map  # noqa: E402
from scripts.accounts.refusals import (  # noqa: E402
    KnownReset as KnownReset, NoResetKnown as NoResetKnown,
    ReadingWindow as ReadingWindow, NoWindow as NoWindow, FreshReading as FreshReading,
    StaleReading as StaleReading, NoReading as NoReading,
)

from scripts.accounts import next_moves
from scripts.accounts.role_map import (  # noqa: E402
    RoleMap as RoleMap, RoleMapChange as RoleMapChange,
    StoredGroupSelection as StoredGroupSelection, StoredGroupSelections as StoredGroupSelections,
    TransitionReason as TransitionReason, RecordedTransitionReason as RecordedTransitionReason,
    UnavailableTransitionReason as UnavailableTransitionReason,
)

if TYPE_CHECKING:
    from scripts.agents.codex_mesh import WorkersRunSwitch
    from scripts.shutdown.inventory import ListedSession, Session, ShowrunnerRegistrations
    from scripts.shutdown.record import SessionShutdown


class PromptTakingMember(Protocol):
    @property
    def because(self) -> Literal["room", "credits", "no fresh reading"]: ...


class MemberNeedingReap(Protocol):
    @property
    def because(self) -> Literal["refused in its transcript", "account out of usage"]: ...
    @property
    def detail(self) -> str: ...


class MovePromptReader(Protocol):
    def PromptSubject(self, session_id: str, login: str, config_dir: Path) -> object: ...
    def prompt_state(self, session: object, now: datetime) -> PromptTakingMember | MemberNeedingReap: ...


class MoveFollower(Protocol):
    def follow(self, login: str, request_id: str) -> int: ...


class RestartRecordLiveness(Protocol):
    def restart_failed_session_running(self, saved: SessionShutdown) -> bool: ...


class ShutdownLocation(Protocol):
    def local_machine(self) -> str: ...


MoveAlerts = Literal["user", "none"]


# Shared means Claude Code reads the entry as user content or cross-session
# state. Everything not listed here stays private to one account.
SHARED = (
    "settings.json",
    "CLAUDE.md",
    "keybindings.json",
    "history.jsonl",
    "commands",
    "skills",
    "agents",
    "output-styles",
    "plugins",
    "projects",
    "sessions",
    "plans",
    "todos",
    "tasks",
    "teams",
    "file-history",
    "shell-snapshots",
    "session-env",
    "paste-cache",
    "uploads",
)

CODEX_SHARED = (
    "config.toml",
    "hooks.json",
    "AGENTS.md",
    "skills",
    "rules",
    "plugins",
    "sessions",
    "archived_sessions",
    "session_index.jsonl",
    "history.jsonl",
    "models_cache.json",
)
CODEX_PRIVATE_NAMES = frozenset({
    "auth.json", "app-server-control", "app-server-daemon", "installation_id",
    "cache", "tmp", ".tmp", "version.json", "shell_snapshots", "packages", "log",
    ".sandbox_migration", ".sqlite-maintenance.lock", "thread-writer-locks",
    "tui-luna-reserve", "tui-thread-reference-capabilities",
    *(f"{database}.sqlite{suffix}"
      for database in ("state_5", "thread_history_1", "goals_1", "logs_2", "queue_1", "memories_1")
      for suffix in ("", "-wal", "-shm")),
})

ALWAYS_LINKED = frozenset({
    "commands", "skills", "agents", "output-styles", "plugins", "projects", "sessions",
    "plans", "todos", "tasks", "teams", "file-history", "shell-snapshots", "session-env",
    "paste-cache", "uploads",
})
CODEX_ALWAYS_LINKED = frozenset({"skills", "rules", "plugins", "sessions", "archived_sessions"})
AT_FDCWD = -100  # Linux: resolve relative paths from the current directory.
RENAME_NOREPLACE = 1  # Linux renameat2: refuse an existing destination.
RENAME_EXCL = 4  # macOS renamex_np: refuse an existing destination.
TOP_LEVEL_SEED_KEYS = (
    "hasCompletedOnboarding",
    "lastOnboardingVersion",
    "installMethod",
    "autoUpdates",
    "shiftEnterKeyBindingInstalled",
    "mcpServers",
    "remoteDialogSeen",
    "hasUsedRemoteControl",
    "hasSeenAutoModeEntryWarning",
    "hasSeenAutoModeOutsideReadPrompt",
    "hasResetAutoModeOptInForDefaultOffer",
)
PROJECT_SEED_KEYS = (
    "hasTrustDialogAccepted",
    "allowedTools",
    "enabledMcpjsonServers",
    "disabledMcpjsonServers",
    "mcpServers",
    "hasClaudeMdExternalIncludesApproved",
    "hasClaudeMdExternalIncludesWarningShown",
)
KNOWN_PRIVATE_NAMES = frozenset(
    {
        ".credentials.json",
        ".claude.json",
        "account-label",
        "daemon",
        "jobs",
        "backups",
        "cache",
        "feedback",
        "telemetry",
        ".last-cleanup",
    }
)
IDENTITY_ENTRIES = (".credentials.json", ".claude.json", "account-label")
IDENTITY_ENTRIES_NOT_SEEDED = (".credentials.json", "account-label")
# What the Codex account does in a production.
CODEX_ROLE = "workers"
# A table cell with nothing to show: the folder has no login, or is not set up.
NO_VALUE = "—"
TABLE_HEADINGS = (
    "Folder", "Label", "Role", "Weekly left", "Weekly resets",
    "On credits", "Credits left", "Read",
)
# A stored reading older than this is read again before the table shows it.
STALE_AFTER = timedelta(minutes=10)


class ProjectSeed(TypedDict, total=False):
    hasTrustDialogAccepted: bool
    allowedTools: list[str]
    enabledMcpjsonServers: list[str]
    disabledMcpjsonServers: list[str]
    mcpServers: dict[str, object]
    hasClaudeMdExternalIncludesApproved: bool
    hasClaudeMdExternalIncludesWarningShown: bool


class ClaudeConfigSeed(TypedDict, total=False):
    hasCompletedOnboarding: bool
    lastOnboardingVersion: str
    installMethod: str
    autoUpdates: bool
    shiftEnterKeyBindingInstalled: bool
    mcpServers: dict[str, object]
    remoteDialogSeen: bool
    hasUsedRemoteControl: bool
    hasSeenAutoModeEntryWarning: bool
    hasSeenAutoModeOutsideReadPrompt: bool
    hasResetAutoModeOptInForDefaultOffer: bool
    projects: dict[str, ProjectSeed]


class AccountsConfiguration(TypedDict, total=False):
    unit: str
    workers: str


class LegacyRoleChange(TypedDict):
    at: str
    role: str
    label: str


class RoleChange(LegacyRoleChange):
    account_label: str


HistoricalRoleChange = LegacyRoleChange | RoleChange


@dataclass(frozen=True)
class NamedAccount:
    """A Claude login selected by its numbered note and launch directory."""

    label: str
    dir: Path


@dataclass(frozen=True)
class CodexWorkersAccount:
    """The workers role may select any Codex home by its numbered account."""

    label: str
    home: Path


ClaudeLaunchAccount = NamedAccount
AccountSelection = ClaudeLaunchAccount | CodexWorkersAccount


@dataclass(frozen=True)
class AccountDirectoryReady:
    """A safe directory logged in as the account its note names."""

    login: str
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountDirectoryLoggedOut:
    """A safe directory that can start Claude to log in."""

    problems: tuple[str, ...]


@dataclass(frozen=True)
class AccountDirectoryWrongLogin:
    """A readable directory logged in as a different account."""

    login: str
    problems: tuple[str, ...]


@dataclass(frozen=True)
class AccountDirectoryNotSetUp:
    """The named directory does not exist yet."""

    problems: tuple[str, ...]


@dataclass(frozen=True)
class AccountDirectoryBrokenLinks:
    """Directory or identity isolation, or a shared link, needs repair."""

    problems: tuple[str, ...]


@dataclass(frozen=True)
class AccountDirectoryUnreadable:
    """Identity or required account-note data cannot establish readiness."""

    problems: tuple[str, ...]


AccountDirectoryReadiness = (
    AccountDirectoryReady | AccountDirectoryLoggedOut | AccountDirectoryWrongLogin
    | AccountDirectoryNotSetUp | AccountDirectoryBrokenLinks | AccountDirectoryUnreadable
)


@dataclass(frozen=True)
class ClaudeDirectoryLogin:
    """The login read from a directory's valid identity configuration."""

    login: str


@dataclass(frozen=True)
class ClaudeDirectoryLoggedOut:
    """No login is stored in the missing or valid identity configuration."""


@dataclass(frozen=True)
class UnreadableClaudeDirectoryIdentity:
    """Existing identity data is inaccessible or malformed."""

    problem: str


ClaudeDirectoryIdentity = ClaudeDirectoryLogin | ClaudeDirectoryLoggedOut | UnreadableClaudeDirectoryIdentity


@dataclass(frozen=True)
class ExpectedAccountLogin:
    """The login declared by the requested label's note."""

    login: str


@dataclass(frozen=True)
class MissingAccountNote:
    """The requested label has no account note."""

    problem: str


@dataclass(frozen=True)
class UnreadableAccountNote:
    """The existing label note cannot establish its expected login."""

    problem: str


AccountNote = ExpectedAccountLogin | MissingAccountNote | UnreadableAccountNote


class QuotaReading(TypedDict):
    account: str
    at: str
    remaining: float
    credits: NotRequired[float]


@dataclass(frozen=True)
class WeeklyQuotaObservation:
    """One account's remaining weekly allowance at an observed time."""

    at: datetime
    remaining: float


@dataclass(frozen=True)
class UnitRoleAssignment:
    """The quota note identity that served unit directors from this time."""

    at: datetime
    label: str


@dataclass(frozen=True)
class AccountRoleBurn:
    """Measured spend while an account served the named roles."""

    label: str
    roles: tuple[agent_accounts.ClaudeRole, ...]
    points: float
    observed_days: float

    @property
    def points_per_day(self) -> float:
        return self.points / self.observed_days


class AccountsError(ValueError):
    """A requested account operation is invalid or unsafe."""


@dataclass(frozen=True)
class AccountStatus:
    """The user-visible state of one default or named account directory."""

    folder: str
    config_dir: Path
    is_named_account: bool
    # False for a label that has an account note and no folder yet.
    is_set_up: bool
    account_is_safe_to_inspect: bool
    label: str
    # The login stored in the folder. Empty when none is stored or it cannot be read.
    login: str
    role: str
    problems: tuple[str, ...]
    unclassified: tuple[str, ...]
    missing_mcp_servers: tuple[str, ...]
    extra_mcp_servers: tuple[str, ...]


@dataclass(frozen=True)
class CodexStatus:
    """One Codex home or a noted account whose home is not set up."""

    folder: str
    home: Path
    is_set_up: bool
    account_is_safe_to_inspect: bool
    label: str
    # The login the home is signed in with. Empty when it has none, and for an account no home holds.
    login: str
    role: str
    problems: tuple[str, ...]
    unclassified: tuple[str, ...]
    startup: agent_accounts.CodexStartupStatus = field(default_factory=agent_accounts.NoCodexStartupFailure)


class CommandLine(argparse.Namespace):
    next_action: str = ""
    group: next_moves.NextMoveGroup = "units"
    percent: float = 0
    hold_mode: next_moves.HoldMode = next_moves.FixedHold(0)
    next_expect: list[str] = []
    next_position: next_moves.GroupPosition = next_moves.GroupPosition(1)
    next_destination: next_moves.GroupPosition = next_moves.GroupPosition(1)
    insertion: next_moves.AppendToGroup | next_moves.InsertAt = next_moves.AppendToGroup()
    entries: str = ""
    action: str = ""
    label: str = ""
    role: str = ""
    since: str = ""
    until: str = ""
    dry_run: bool = False
    alerts: MoveAlerts = "user"
    claude_args: list[str] = []
    probe: bool = False
    session: str = ""
    source: str = ""
    target: str = ""
    explain: bool = False
    why: str = ""
    detail: bool = False
    as_json: bool = False
    stored: bool = False
    change: str = ""




class SwapDirectorOwner(TypedDict):
    kind: Literal["director"]
    session_id: str


class SwapNoDirector(TypedDict):
    kind: Literal["no director"]


class SwapUnitSession(TypedDict):
    kind: Literal["unit"]
    session_id: str
    name: str


class SwapWorkerSession(TypedDict):
    kind: Literal["worker"]
    session_id: str
    name: str
    owner: SwapDirectorOwner | SwapNoDirector


class SwapOtherSession(TypedDict):
    kind: Literal["showrunner", "top-level"]
    session_id: str
    name: str


SwapSession = SwapUnitSession | SwapWorkerSession | SwapOtherSession


class SwapUnattributedSession(TypedDict):
    name: str
    pid: int
    reason: str


class SwapInventory(TypedDict):
    """The account inventory fields needed to prove every session can move."""

    machine: str
    login: str
    label: str
    sessions: list[SwapSession]
    unattributed: list[SwapUnattributedSession]


@dataclass(frozen=True)
class SwapDryRun:
    """Validated commands, with neither a role write nor a shutdown started."""

    role_command: tuple[str, ...]
    down_command: tuple[str, ...]


@dataclass(frozen=True)
class SwapStarted:
    """The new role is selected and shutdown accepted the restart request."""

    old_label: str
    target_label: str
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SwapRolledBack:
    """Shutdown refused before stopping sessions and the old role is restored."""

    old_label: str
    stdout: str
    stderr: str
    returncode: int


SwapOutcome = SwapDryRun | SwapStarted | SwapRolledBack


@dataclass(frozen=True)
class WorkersSwapDryRun:
    """The target is ready; these run notices describe the proposed move."""

    lines: tuple[str, ...]


@dataclass(frozen=True)
class WorkersSwapStarted:
    """The workers role has moved; each interrupted worker reports its resume outcome."""

    lines: tuple[str, ...]


WorkersSwapOutcome = WorkersSwapDryRun | WorkersSwapStarted


@dataclass(frozen=True)
class SelectableCodexAccount:
    """The safe signed-in home and identity that passed the target check."""

    home: Path
    login: str
    label: str


def accounts_home() -> Path:
    """The account root selected from HOME for this call."""
    return Path.home()


def main_config_dir() -> Path:
    return accounts_home() / ".claude"


def accounts_config_file() -> Path:
    return worker_names.accounts_config_file()


def roles_history_file() -> Path:
    return accounts_home() / ".local" / "state" / "accounts" / "roles.jsonl"


def _label_is(label: str, tool: str) -> bool:
    parsed = note_file.parse_account_label(label)
    return isinstance(parsed, note_file.NumberedAccountLabel) and parsed.tool == tool


def account_dir(label: str) -> Path:
    parsed = note_file.parse_account_label(label)
    if not isinstance(parsed, note_file.NumberedAccountLabel) or parsed.tool != "claude":
        raise AccountsError(f"accounts: {label} is not a claudeN label")
    return accounts_home() / f".claude{parsed.number}"


def codex_setup_home(label: str) -> Path:
    parsed = note_file.parse_account_label(label)
    if not isinstance(parsed, note_file.NumberedAccountLabel) or parsed.tool != "codex":
        raise AccountsError(f"accounts: {label} is not a codexN label")
    return accounts_home() / f".codex{parsed.number}"


def _codex_account_home(label: str) -> Path:
    try:
        return agent_accounts.codex_account_home(label).home
    except ValueError as error:
        raise AccountsError(str(error)) from error


def _is_expected_link(path: Path, target: Path) -> bool:
    try:
        return path.is_symlink() and os.readlink(path) == str(target)
    except OSError:
        return False


def _real_entry_problem(path: Path, shared_home: str = "~/.claude") -> str:
    return (
        f"accounts: {path} is a real file; move it aside or merge it into {shared_home}"
    )


def _move_shared_entry(source: Path, destination: Path) -> None:
    """Move shared content atomically without ever replacing another entry."""
    library = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform.startswith("linux"):
            rename = cast(Callable[[int, bytes, int, bytes, int], int], library.renameat2)
            result = rename(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), RENAME_NOREPLACE)
        elif sys.platform == "darwin":
            rename_exclusive = cast(Callable[[bytes, bytes, int], int], library.renamex_np)
            result = rename_exclusive(os.fsencode(source), os.fsencode(destination), RENAME_EXCL)
        else:
            raise OSError(errno.ENOTSUP, "atomic no-replace moves are unavailable")
    except AttributeError as error:
        raise OSError(errno.ENOSYS, "atomic no-replace moves are unavailable") from error
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def _shared_clash_problem(path: Path, target: Path, names: Sequence[str]) -> str:
    return (f"accounts: repair {path}: resolve clashing entries {', '.join(sorted(names))} "
            + f"with {target}, then rerun")


def _folder_merge_problem(path: Path, target: Path) -> str:
    if not target.is_dir():
        return f"accounts: repair {path}: move its contents into a shared folder at {target}, then rerun"
    clashes = [entry.name for entry in path.iterdir() if os.path.lexists(target / entry.name)]
    return _shared_clash_problem(path, target, clashes) if clashes else ""


def _shared_move_problem(path: Path, target: Path, error: OSError) -> str:
    if error.errno in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}:
        return (f"accounts: repair {path}: atomic no-replace moves to {target} are unavailable; "
                + "move the entries without replacing shared content, then rerun")
    return f"accounts: repair {path}: could not move into {target}: {error}; then rerun"


def _repair_link(path: Path, target: Path, shared_home: str = "~/.claude") -> list[str]:
    try:
        if path.is_symlink():
            if _is_expected_link(path, target):
                return []
            path.unlink()
        elif path.exists():
            if not os.path.lexists(target):
                try:
                    _move_shared_entry(path, target)
                except OSError as error:
                    if _is_expected_link(path, target):
                        return []
                    return [_shared_clash_problem(path, target, (path.name,)) if isinstance(error, FileExistsError)
                            else _shared_move_problem(path, target, error)]
                path.symlink_to(target, target_is_directory=target.is_dir())
                print(f"accounts: moved {path} into {target} and linked it", file=sys.stderr)
                return []
            if not path.is_dir():
                return [_real_entry_problem(path, shared_home)]
            entries = sorted(path.iterdir())
            if entries:
                problem = _folder_merge_problem(path, target)
                if problem:
                    return [problem]
                moved: list[str] = []
                for entry in entries:
                    try:
                        _move_shared_entry(entry, target / entry.name)
                    except OSError as error:
                        if _is_expected_link(path, target):
                            return []
                        problem = (_shared_clash_problem(path, target, (entry.name,))
                                   if isinstance(error, FileExistsError)
                                   else _shared_move_problem(path, target, error))
                        if moved:
                            remaining = ", ".join(item.name for item in entries if item.name not in moved)
                            problem += (f"; already moved {', '.join(moved)} into {target}; "
                                        + f"remaining in {path}: {remaining}; rerun to finish")
                        return [problem]
                    moved.append(entry.name)
                path.rmdir()
                path.symlink_to(target, target_is_directory=True)
                print(f"accounts: merged {path} into {target} and linked it", file=sys.stderr)
                return []
            path.rmdir()
        path.symlink_to(target, target_is_directory=target.is_dir())
        return []
    except OSError:
        if _is_expected_link(path, target):
            return []
        raise


def _shared_repair_problems(
    destination: Path, source_dir: Path, shared: tuple[str, ...], folders: frozenset[str],
) -> list[str]:
    """Judge shared entries without performing any of their possible repairs."""
    if not source_dir.is_dir():
        return _shared_link_problems(destination, source_dir, shared, folders)
    problems: list[str] = []
    for name in shared:
        path, target = destination / name, source_dir / name
        try:
            if target.is_symlink() and not target.exists():
                problems.extend(_shared_link_problems(destination, source_dir, (name,), frozenset()))
                continue
            if path.is_symlink():
                if (not os.path.lexists(target) and name not in folders
                        and not _is_expected_link(path, target)):
                    problems.extend(_shared_link_problems(destination, source_dir, (name,), frozenset()))
                continue
            if not path.exists() or not os.path.lexists(target):
                continue
            if not path.is_dir():
                problems.append(_real_entry_problem(path, f"~/{source_dir.name}"))
            elif any(path.iterdir()):
                problem = _folder_merge_problem(path, target)
                if problem:
                    problems.append(problem)
        except OSError as error:
            problems.append(f"accounts: could not inspect {path}: {error}")
    return problems


def _repair_shared_links(
    destination: Path, source_dir: Path, shared: tuple[str, ...], folders: frozenset[str],
) -> list[str]:
    """Repair entries inside an existing shared home; never create an empty shared file."""
    if (destination == source_dir or destination.is_symlink() or not destination.is_dir()
            or not source_dir.is_dir()):
        return []
    problems: list[str] = []
    for name in shared:
        target = source_dir / name
        path = destination / name
        try:
            if not os.path.lexists(target):
                if path.exists() and not path.is_symlink():
                    problems.extend(_repair_link(path, target, f"~/{source_dir.name}"))
                    continue
                if name in folders:
                    target.mkdir(mode=0o700, exist_ok=True)
                    target.chmod(0o700)
                else:
                    if _is_expected_link(path, target):
                        try:
                            path.unlink()
                        except OSError:
                            if os.path.lexists(path):
                                raise
                    continue
            problems.extend(_repair_link(path, target, f"~/{source_dir.name}"))
        except OSError as error:
            if not _is_expected_link(path, target):
                problems.append(f"accounts: could not link {path}: {error}")
    return problems


def _identity_link_problems(
    config_dir: Path,
    entries: tuple[str, ...] = IDENTITY_ENTRIES,
) -> list[str]:
    return [
        f"accounts: {path} is a link; identity stays in each account dir"
        for name in entries
        if (path := config_dir / name).is_symlink()
    ]


def _read_json_object(path: Path) -> dict[str, object]:
    decoded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(decoded, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, object], decoded)


def _filtered_seed(source: dict[str, object]) -> ClaudeConfigSeed:
    seed: dict[str, object] = {
        key: source[key] for key in TOP_LEVEL_SEED_KEYS if key in source
    }
    raw_projects = source.get("projects")
    if isinstance(raw_projects, dict):
        projects: dict[str, ProjectSeed] = {}
        for project_path, raw_project in cast(
            dict[object, object], cast(object, raw_projects)
        ).items():
            if not isinstance(project_path, str) or not isinstance(raw_project, dict):
                continue
            project = cast(dict[str, object], cast(object, raw_project))
            filtered: dict[str, object] = {
                key: project[key] for key in PROJECT_SEED_KEYS if key in project
            }
            projects[project_path] = cast(ProjectSeed, cast(object, filtered))
        seed["projects"] = projects
    return cast(ClaudeConfigSeed, cast(object, seed))


def _seed_config(destination: Path) -> list[str]:
    if destination.is_symlink():
        return [
            f"accounts: {destination} is a link; identity stays in each account dir"
        ]
    if destination.exists():
        return []
    source = accounts_home() / ".claude.json"
    created = False
    try:
        seed = _filtered_seed(_read_json_object(source))
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(seed, output, indent=2, sort_keys=True)
            _ = output.write("\n")
        destination.chmod(0o600)
    except (OSError, ValueError, TypeError) as error:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        return [f"accounts: could not seed {destination} from {source}: {error}"]
    return []


def setup(label: str) -> list[str]:
    """Build or repair one named account, returning every refusal or failure."""
    label = note_file.canonical_label(label)
    if _label_is(label, "codex"):
        return _setup_codex_home(label)
    destination = account_dir(label)
    problems: list[str] = []
    if destination.is_symlink():
        return [
            f"accounts: {destination} is a link; an account dir must be a real directory"
        ]
    if destination.exists() and not destination.is_dir():
        return [f"accounts: {destination} is not a directory"]
    try:
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.chmod(0o700)
    except OSError as error:
        return [f"accounts: could not create {destination}: {error}"]

    problems.extend(
        _identity_link_problems(destination, IDENTITY_ENTRIES_NOT_SEEDED)
    )
    problems.extend(_repair_shared_links(
        destination, main_config_dir().absolute(), SHARED, ALWAYS_LINKED,
    ))

    problems.extend(_seed_config(destination / ".claude.json"))
    return problems


def _link_problems(destination: Path) -> list[str]:
    return _shared_link_problems(destination, main_config_dir().absolute(), SHARED, ALWAYS_LINKED)


def _shared_link_problems(
    destination: Path, source_dir: Path, shared: tuple[str, ...], required: frozenset[str],
) -> list[str]:
    problems: list[str] = []
    for name in shared:
        target = source_dir / name
        path = destination / name
        if name in required and not os.path.lexists(target) and (
                not os.path.lexists(path) or _is_expected_link(path, target)):
            continue
        if name not in required and not os.path.lexists(target) and not os.path.lexists(path):
            continue
        if path.is_symlink():
            if not _is_expected_link(path, target):
                try:
                    actual = os.readlink(path)
                except OSError:
                    actual = "unreadable"
                problems.append(
                    f"accounts: {path} points to {actual}, not {target}"
                )
            elif not path.exists():
                problems.append(f"accounts: {path} is broken")
        elif path.exists():
            problems.append(_real_entry_problem(path, f"~/{source_dir.name}"))
        else:
            problems.append(f"accounts: {path} is missing")
    return problems


def _directory_identity(config_dir: Path) -> ClaudeDirectoryIdentity:
    path = agent_accounts.claude_config_file(config_dir)
    try:
        values = _read_json_object(path)
    except FileNotFoundError:
        return ClaudeDirectoryLoggedOut()
    except (OSError, ValueError, UnicodeError) as error:
        return UnreadableClaudeDirectoryIdentity(f"accounts: could not read {path}: {error}")
    oauth = values.get("oauthAccount")
    if oauth is None:
        return ClaudeDirectoryLoggedOut()
    if isinstance(oauth, dict):
        login = cast(dict[str, object], oauth).get("emailAddress")
        if isinstance(login, str) and login.strip():
            return ClaudeDirectoryLogin(login)
    return UnreadableClaudeDirectoryIdentity(f"accounts: {path} has an invalid oauthAccount identity")


_account_notes_directory = note_file.notes_dir


def _account_note(label: str) -> AccountNote:
    label = note_file.canonical_label(label)
    path = note_file.note_path(label)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MissingAccountNote(f"accounts: {label} has no account note at {path}")
    except (OSError, UnicodeError) as error:
        return UnreadableAccountNote(f"accounts: could not read {path}: {error}")
    close = text.find("\n---", 3)
    if text.startswith("---\n") and close != -1:
        parsed = note_file.parse_account_label(label)
        assert isinstance(parsed, note_file.NumberedAccountLabel)
        login = note_file.Note(path, parsed.tool, text, 4, close, parsed.label).get("login")
        if isinstance(login, str) and login.strip() and login not in {"null", "~"}:
            return ExpectedAccountLogin(login)
    return UnreadableAccountNote(f"accounts: {path} has no readable login in its frontmatter")


def _private_identity_problems(destination: Path) -> list[str]:
    problems: list[str] = []
    for name in (".credentials.json", "account-label"):
        path = destination / name
        if path.is_symlink():
            continue
        try:
            if name == ".credentials.json":
                _ = _read_json_object(path)
            else:
                _ = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeError) as error:
            problems.append(f"accounts: could not read {path}: {error}")
    return problems


def account_directory_readiness(label: str) -> AccountDirectoryReadiness:
    """Read launch safety and login matching once, retaining every diagnostic."""
    label = note_file.canonical_label(label)
    selected = NamedAccount(label, account_dir(label))
    return _inspect_account_directory(selected, _account_note(label))


def _inspect_account_directory(
    selected: NamedAccount, expected: AccountNote, *, allow_shared_repair: bool = False,
) -> AccountDirectoryReadiness:
    label = selected.label
    destination = selected.dir
    if destination.is_symlink():
        return AccountDirectoryBrokenLinks((
            f"accounts: {destination} is a link; an account dir must be a real directory",
        ))
    try:
        metadata = destination.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            return AccountDirectoryBrokenLinks((f"accounts: {destination} is not a directory",))
    except FileNotFoundError:
        return AccountDirectoryNotSetUp((f"accounts: {destination} is missing",))
    except OSError as error:
        return AccountDirectoryUnreadable((f"accounts: could not inspect {destination}: {error}",))
    identity_problems = _identity_link_problems(destination)
    shared_problems = (_shared_repair_problems(destination, main_config_dir().absolute(), SHARED, ALWAYS_LINKED)
                       if allow_shared_repair else _link_problems(destination))
    links = [*identity_problems, *shared_problems]
    private_problems = _private_identity_problems(destination)
    problems = [*links, *private_problems]
    # Do not follow an unsafe identity link, even while collecting diagnostics.
    if (destination / ".claude.json").is_symlink():
        if not isinstance(expected, ExpectedAccountLogin):
            problems.append(expected.problem)
        return AccountDirectoryBrokenLinks(tuple(problems))
    identity = _directory_identity(destination)
    if isinstance(identity, UnreadableClaudeDirectoryIdentity):
        problems.append(identity.problem)
    if isinstance(expected, UnreadableAccountNote):
        problems.append(expected.problem)
    if isinstance(identity, ClaudeDirectoryLoggedOut):
        problems.append(f"accounts: {destination} is not logged in")
    if isinstance(expected, MissingAccountNote):
        problems.append(expected.problem)
    wrong_login = False
    if isinstance(identity, ClaudeDirectoryLogin) and isinstance(expected, ExpectedAccountLogin):
        wrong_login = identity.login.casefold() != expected.login.casefold()
        if wrong_login:
            problems.append(
                f"accounts: ~/.claude{_label_number(label)} is logged in as {identity.login}, not {label}; see Log out and in in ~/.claude/docs/accounts.md"
            )
    if links:
        return AccountDirectoryBrokenLinks(tuple(problems))
    if (
        isinstance(identity, UnreadableClaudeDirectoryIdentity)
        or isinstance(expected, UnreadableAccountNote)
        or private_problems
        or isinstance(identity, ClaudeDirectoryLogin) and isinstance(expected, MissingAccountNote)
    ):
        return AccountDirectoryUnreadable(tuple(problems))
    if isinstance(identity, ClaudeDirectoryLoggedOut):
        return AccountDirectoryLoggedOut(tuple(problems))
    if wrong_login:
        return AccountDirectoryWrongLogin(identity.login, tuple(problems))
    return AccountDirectoryReady(identity.login)


def check(label: str) -> list[str]:
    """Return every reason a numbered account is not fit to launch."""
    label = note_file.canonical_label(label)
    refuse_default(label)
    if _label_is(label, "codex"):
        return _check_codex_home(label, accounts_home() / ".codex")
    parsed = numbered_account_label(label, "claude")
    expected = _account_note(parsed.label)
    directory = account_dir(parsed.label)
    return list(_inspect_account_directory(NamedAccount(label, directory), expected).problems)


def _setup_codex_home(label: str) -> list[str]:
    destination = codex_setup_home(label)
    expected = _account_note(label)
    if not isinstance(expected, ExpectedAccountLogin):
        return [expected.problem]
    for home in _codex_identity_homes(accounts_home() / ".codex"):
        if home != destination and _codex_home_login(home).casefold() == expected.login.casefold():
            return [
                f"accounts: cannot set up {label}: {_codex_folder(home)} already holds its login; "
                + "two homes on one login break token refresh"
            ]
    problems = _link_codex_home(label)
    if not problems:
        prepared = _prepare_codex_home(label)
        if isinstance(prepared, agent_accounts.CodexHomePreparationFailed):
            problems.append(f"accounts: {prepared.problem}")
    return problems


def _prepare_codex_home(label: str) -> agent_accounts.CodexHomePreparation:
    destination = codex_setup_home(label)
    print(
        f"Wait for {label} ({_codex_folder(destination)}) to finish its shared session index "
        + "and answer initialize; an interrupted index can hold startup for 15 minutes, "
        + f"then indexing must finish; limit {agent_accounts.CODEX_HOME_PREPARATION_SECONDS / 60:g} minutes.",
        flush=True,
    )
    return asyncio.run(agent_accounts.prepare_codex_home(destination))


def _link_codex_home(label: str) -> list[str]:
    """Prepare private storage and shared links without starting a Codex process."""
    destination = codex_setup_home(label)
    if destination.is_symlink():
        return [f"accounts: {destination} is a link; an account home must be a real directory"]
    if destination.exists() and not destination.is_dir():
        return [f"accounts: {destination} is not a directory"]
    try:
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.chmod(0o700)
    except OSError as error:
        return [f"accounts: could not create {destination}: {error}"]
    problems = _codex_private_link_problems(destination)
    problems.extend(_repair_shared_links(
        destination, (accounts_home() / ".codex").absolute(), CODEX_SHARED, CODEX_ALWAYS_LINKED,
    ))
    return problems


def _codex_private_link_problems(home: Path) -> list[str]:
    return [
        f"accounts: {home / name} is a link; sign-in and home state stay in each account home"
        for name in sorted(CODEX_PRIVATE_NAMES) if (home / name).is_symlink()
    ]


def _codex_home_login(home: Path) -> str:
    """Read a home identity without following a linked home or sign-in file."""
    if home.is_symlink() or (home / "auth.json").is_symlink():
        return ""
    return agent_accounts.codex_email_on_disk(home) or ""


def _codex_identity_homes(workers_home: Path) -> list[Path]:
    """Check every listed home and any other home this process knows about."""
    homes = agent_accounts.codex_homes(workers_home)
    process_home = agent_accounts.codex_home()
    if process_home not in homes:
        homes.append(process_home)
    return homes


def _codex_folder(home: Path) -> str:
    return f"~/{home.relative_to(accounts_home())}" if home.is_relative_to(accounts_home()) else str(home)


def _check_codex_home(
    label: str, workers_home: Path, *, allow_shared_repair: bool = False,
) -> list[str]:
    destination = _codex_account_home(label)
    folder = _codex_folder(destination)
    if destination.is_symlink():
        return [f"accounts: {folder} is a link; an account home must be a real directory"]
    if not destination.exists():
        return [f"accounts: {folder} is missing"]
    if not destination.is_dir():
        return [f"accounts: {folder} is not a directory"]
    problems = (_check_default_codex_home(workers_home) if destination == accounts_home() / ".codex"
                else _shared_repair_problems(destination, (accounts_home() / ".codex").absolute(), CODEX_SHARED, CODEX_ALWAYS_LINKED)
                if allow_shared_repair
                else _shared_link_problems(destination, (accounts_home() / ".codex").absolute(),
                                           CODEX_SHARED, CODEX_ALWAYS_LINKED))
    problems.extend(_codex_private_link_problems(destination))
    if destination != accounts_home() / ".codex":
        try:
            with os.scandir(destination):
                pass
        except OSError as error:
            problems.append(f"accounts: could not inspect {destination}: {error}")
    expected = _account_note(label)
    if not isinstance(expected, ExpectedAccountLogin):
        problems.append(expected.problem)
    login = _codex_home_login(destination)
    if not login:
        problems.append(f"accounts: {folder} is not logged in")
    elif isinstance(expected, ExpectedAccountLogin) and login.casefold() != expected.login.casefold():
        problems.append(f"accounts: {folder} is logged in as {login}, not {label}")
    if login:
        for home in _codex_identity_homes(workers_home):
            if home != destination and _codex_home_login(home).casefold() == login.casefold():
                problems.append(f"accounts: {folder} and {_codex_folder(home)} are logged in as {login}; two homes on one login break token refresh")
    return problems


def _check_default_codex_home(workers_home: Path) -> list[str]:
    home = accounts_home() / ".codex"
    if home.is_symlink():
        return ["accounts: ~/.codex is a link; use a real home"]
    if not home.is_dir():
        return ["accounts: ~/.codex is not a directory; set up ~/.codex first"]
    login = _codex_home_login(home)
    if not login:
        return ["accounts: ~/.codex has no readable sign-in; sign in there before swapping"]
    return [
        f"accounts: ~/.codex and {_codex_folder(other)} hold {login}; keep each login in one home"
        for other in _codex_identity_homes(workers_home)
        if other != home and _codex_home_login(other).casefold() == login.casefold()
    ]


@dataclass(frozen=True)
class PreparedAccount:
    """Account launch variables to apply to a caller's own environment."""
    variables: dict[str, str]
    remove: frozenset[str]


@dataclass(frozen=True)
class AccountRefused:
    """The selected account cannot safely prepare this launch."""
    reason: str


def prepared_account(label: str, *, resume: bool) -> PreparedAccount | AccountRefused:
    """Repair and prepare a numbered account for an actual launch."""
    return _prepared_account(label, resume=resume, repair=True)


def prepared_account_preview(label: str, *, resume: bool) -> PreparedAccount | AccountRefused:
    """Judge a launch after possible repairs without changing the account."""
    return _prepared_account(label, resume=resume, repair=False)


def _prepared_account(label: str, *, resume: bool, repair: bool) -> PreparedAccount | AccountRefused:
    label = note_file.canonical_label(label)
    try:
        refuse_default(label)
        if label in ("unit", "showrunner", "units", "showrunners"):
            selected = role_account("unit" if label in ("unit", "units") else "showrunner")
            expected = _account_note(selected.label)
        else:
            parsed = numbered_account_label(label, "claude")
            expected = _account_note(parsed.label)
            selected = NamedAccount(parsed.label, account_dir(parsed.label))
    except AccountsError as error:
        return AccountRefused(str(error))
    remove = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
    if repair:
        problems = _repair_shared_links(selected.dir, main_config_dir().absolute(), SHARED, ALWAYS_LINKED)
        if problems:
            return AccountRefused(problems[0])
    readiness = _inspect_account_directory(selected, expected, allow_shared_repair=not repair)
    if isinstance(readiness, AccountDirectoryNotSetUp):
        return AccountRefused(
            f'accounts: ~/.claude{_label_number(selected.label)} is not set up; /showrunner:accounts setup "{selected.label}" first'
        )
    if not isinstance(readiness, (AccountDirectoryReady, AccountDirectoryLoggedOut)) or (
        resume and isinstance(readiness, AccountDirectoryLoggedOut)
    ):
        return AccountRefused(readiness.problems[0])
    return PreparedAccount(account.named_account_environment(selected.dir), remove)


class LaunchShellEnvironment(Protocol):
    @property
    def env(self) -> Mapping[str, str]: ...


class LaunchShellRefusal(Protocol):
    @property
    def reason(self) -> str: ...


class NixShellLoader(Protocol):
    Loaded: type[LaunchShellEnvironment]
    NoEnvrc: type[LaunchShellEnvironment]
    Refused: type[LaunchShellRefusal]

    def ensure(self, directory: Path, base_env: Mapping[str, str]) -> LaunchShellEnvironment | LaunchShellRefusal: ...


class LaunchShellError(AccountsError):
    """The folder's shell could not be loaded for this launch."""


def launch_shell_environment(directory: Path) -> dict[str, str]:
    """Load the shared helper only when an account is about to launch."""
    try:
        loader = cast(NixShellLoader, cast(object, import_module("scripts.lib.nix_env")))
    except ImportError as error:
        raise LaunchShellError("accounts: nix shell helper unavailable (scripts/lib/nix_env.py); update the shared scripts") from error
    base_env = dict(os.environ)
    result = loader.ensure(directory, base_env)
    if isinstance(result, loader.Refused):
        raise LaunchShellError(result.reason)
    if isinstance(result, (loader.Loaded, loader.NoEnvrc)):
        environment = dict(result.env)
        # The folder's shell must not change the launcher's account home.
        if "HOME" in base_env:
            environment["HOME"] = base_env["HOME"]
        else:
            _ = environment.pop("HOME", None)
        return environment
    raise LaunchShellError("accounts: nix shell helper returned an unsupported result")


def resolve_program(tool: Literal["claude", "codex"], *, environment: Mapping[str, str] = os.environ) -> str:
    """Find the agent itself, without loading the launch shell a second time."""
    installed = accounts_home() / ".local" / "bin" / tool
    directories = environment.get("PATH", os.defpath).split(os.pathsep)
    for directory in [*directories, str(installed.parent)]:
        found = shutil.which(tool, path=directory or os.curdir)
        if not found:
            continue
        program = Path(found)
        # Shell launchers invoke the shared helper. Agent scripts must still work.
        try:
            with program.open("rb") as source:
                if source.read(2) == b"#!":
                    script = source.read()
                    if b"nix_env.py" in script or b"lib.nix_env" in script:
                        continue
        except OSError:
            # An executable does not need read permission to run.
            pass
        return str(program.absolute())
    raise FileNotFoundError(f"{tool} program not found (looked in PATH and {installed})")


def _exec_codex_account(label: str, arguments: list[str]) -> None:
    """Launch Codex on a checked account, including the stored workers selection."""
    selected_label = role_account("workers").label if label == "workers" else label
    target = check_codex_target(selected_label, accounts_home() / ".codex")
    environment = launch_shell_environment(Path.cwd())
    environment["CODEX_HOME"] = str(target.home)
    try:
        program = resolve_program("codex", environment=environment)
        os.execvpe(program, [program, *arguments], environment)
    except OSError as error:
        raise AccountsError(f"accounts: could not start codex: {error}") from error


@dataclass(frozen=True)
class LoginRenewed:
    """A probe ran and usage no longer reports an expired login."""


@dataclass(frozen=True)
class LoginRenewalFailed:
    detail: str


@dataclass(frozen=True)
class LoginRenewalAttempt:
    at: datetime
    result: LoginRenewed | LoginRenewalFailed


def login_renewals_file() -> Path:
    return accounts_home() / ".local" / "state" / "accounts" / "login_renewals.json"


def login_renewal_attempts() -> dict[str, LoginRenewalAttempt]:
    """Read local probe results. Discard damaged state so the next attempt replaces it."""
    try:
        raw = cast(object, json.loads(login_renewals_file().read_text()))
        if not isinstance(raw, dict):
            return {}
        attempts: dict[str, LoginRenewalAttempt] = {}
        for label, value in cast(dict[str, object], raw).items():
            if not isinstance(value, dict):
                return {}
            item = cast(dict[str, object], value)
            at, outcome, detail = item.get("at"), item.get("outcome"), item.get("detail")
            if not isinstance(at, str) or outcome not in ("renewed", "failed") or not isinstance(detail, str):
                return {}
            stamp = datetime.fromisoformat(at)
            if stamp.tzinfo is None:
                return {}
            attempts[label] = LoginRenewalAttempt(stamp,
                LoginRenewed() if outcome == "renewed" else LoginRenewalFailed(detail))
        return attempts
    except (OSError, ValueError):
        return {}


def renew_login(label: str) -> LoginRenewed | LoginRenewalFailed:
    """Run a quiet probe, then keep its new usage reading. Never expose probe output."""
    directory = claude_account_dir(label)
    try:
        probe = subprocess.run([sys.executable, str(SCRIPTS / "accounts" / "accounts.py"),
            "exec", "--probe", label, "-p", "hello"], cwd="/tmp", stdin=subprocess.DEVNULL,
            timeout=120, capture_output=True)
    except subprocess.TimeoutExpired:
        return LoginRenewalFailed("timed out after 120 s")
    except OSError:
        return LoginRenewalFailed("probe could not start")
    if probe.returncode != 0:
        # The exec wrapper uses status 2 when it cannot find the program.
        if probe.returncode == 2:
            try:
                _ = resolve_program("claude")
            except FileNotFoundError as error:
                return LoginRenewalFailed(str(error))
        return LoginRenewalFailed(f"probe exited with status {probe.returncode}")
    try:
        report = agent_accounts.claude_live(directory)
        usage_store.save([report], datetime.now(timezone.utc))
    except (OSError, ValueError, RuntimeError):
        return LoginRenewalFailed("usage could not be read")
    if (report.usage_read_outcome == agent_accounts.UsageReadOutcome.STALE_TOKEN
            or isinstance(report.weekly_read_skip, agent_accounts.StaleTokenSkip)):
        return LoginRenewalFailed("usage still shows an expired login")
    return LoginRenewed()


def exec_account(label: str, arguments: list[str], *, probe: bool = False) -> None:
    """Replace this process with the selected Claude or Codex account."""
    label = note_file.canonical_label(label)
    if label == "workers" or _label_is(label, "codex"):
        if probe:
            raise AccountsError("accounts: --probe is only supported for Claude accounts")
        _exec_codex_account(label, arguments)
        return
    if label == "claude":
        prepared = PreparedAccount({}, frozenset({
            "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_REMOTE_MEMORY_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
        }))
    else:
        prepared = prepared_account(label, resume="--resume" in arguments)
    if isinstance(prepared, AccountRefused):
        raise AccountsError(prepared.reason)
    environment = launch_shell_environment(Path.cwd())
    for name in prepared.remove:
        _ = environment.pop(name, None)
    environment.update(prepared.variables)
    if probe:
        from scripts.herdr.herdr import UNIT_MARKS

        environment[PROBE_ENV] = "1"
        for name in UNIT_MARKS:
            _ = environment.pop(name, None)
    try:
        program = resolve_program("claude", environment=environment)
        if sys.stdin.isatty() and "RELAUNCH_SUPERVISED" not in environment:
            os.execvpe(sys.executable, [sys.executable, str(SCRIPTS / "lib" / "relaunch.py"),
                                      "run", "--", program, *arguments], environment)
        else:
            os.execvpe(program, [program, *arguments], environment)
    except OSError as error:
        raise AccountsError(f"accounts: could not start claude: {error}") from error


def _validate_role(role: str) -> None:
    if role not in {"unit", "showrunner", "workers"}:
        raise AccountsError(f"accounts: {role} is not a role; only unit, showrunner and workers are supported")


def read_role_map() -> RoleMap:
    try:
        return role_map.read_role_map()
    except role_map.RoleMapUnavailable as error:
        raise AccountsError(str(error)) from error


def write_role_map(change: RoleMapChange | role_map.WorkersClaudeRouteChange) -> None:
    try:
        role_map.write_role_map(change)
    except role_map.RoleMapWriteRefused as error:
        raise AccountsError(str(error)) from error


def _workers_home() -> Path:
    """Convert an unreadable workers role into the account tool's error."""
    try:
        return agent_accounts.workers_home()
    except (OSError, ValueError) as error:
        raise AccountsError(str(error)) from error


@overload
def role_account(role: Literal["unit", "showrunner"]) -> ClaudeLaunchAccount: ...


@overload
def role_account(role: Literal["workers"]) -> CodexWorkersAccount: ...


@overload
def role_account(role: str) -> AccountSelection: ...


def role_account(role: str) -> AccountSelection:
    """Resolve a launch role to its numbered account."""
    _validate_role(role)
    if role == "workers":
        home = _workers_home()
        label = agent_accounts.codex_home_role_label(home)
        return CodexWorkersAccount(label, home)
    group: ClaudeGroup = "units" if role == "unit" else "showrunners"
    selected = group_label(group_accounts(), group)
    if isinstance(selected, GroupNotConfigured):
        raise AccountsError("accounts: " + group_configuration_line(group))
    return _claude_role_selection(group, selected)


def _claude_role_selection(group: ClaudeGroup, label: str) -> NamedAccount:
    directory = account_dir(label)
    state = "not set up" if not directory.is_dir() else "not logged in" if account.claude_account(directory) is None else ""
    if state:
        step = (f"run accounts.py setup {label}" if state == "not set up"
                else f"run CLAUDE_CONFIG_DIR={shlex.quote(str(directory))} claude and /login")
        raise AccountsError(f"accounts: {group} is on {label}, which is {state} on this machine; {step}")
    return NamedAccount(label, claude_account_dir(label))


def account_for_group_launch(role: Literal["unit", "showrunner"], *, resume: bool,
                             target: str = "", config_dir: str = "", source_fallback: bool = False) -> ClaudeLaunchAccount:
    """Use the group account, except for a moved unit restored on its source."""
    if source_fallback:
        if role != "unit" or not resume or not target or config_dir:
            raise AccountsError("a source fallback needs a unit restore and its source account")
        selected = NamedAccount(target, claude_account_dir(target))
    else:
        selected = role_account(role)
    label = selected.label
    directory = selected.dir
    group = "units" if role == "unit" else "showrunners"
    if target and config_dir:
        raise AccountsError("give only one of --account and --config-dir")
    requested = claude_account_dir(target) if target else Path(config_dir).expanduser() if config_dir else directory
    if requested.resolve() != directory.resolve():
        raise AccountsError(f"accounts: {group} account is {label}; --account and --config-dir must name {label}")
    prepared = prepared_account(label, resume=resume)
    if isinstance(prepared, AccountRefused):
        raise AccountsError(prepared.reason)
    return selected


@dataclass(frozen=True)
class LegacyHomeLaunch:
    """This machine still launches unscoped Claude with no account setting."""


@role_map.role_map_command
def account_for_unscoped_launch(environment: Mapping[str, str], *, workers: bool = False) -> ClaudeLaunchAccount | LegacyHomeLaunch:
    """Use a named workers route, else retain the caller or showrunners choice."""
    if workers:
        target = read_role_map().workers_claude
        if isinstance(target, role_map.NamedWorkersClaudeAccount) and datetime.now(timezone.utc) < target.until:
            selected = claude_workers_account(target.label)
            prepared = prepared_account(target.label, resume=True)
            if isinstance(prepared, AccountRefused):
                raise AccountsError(prepared.reason)
            return selected
    configured = environment.get("CLAUDE_CONFIG_DIR", "")
    if not configured:
        if account.claude_account(main_config_dir()) is not None:
            return LegacyHomeLaunch()
        return account_for_group_launch("showrunner", resume=False)
    identity = account_identity(Path(configured).expanduser())
    if not isinstance(identity, agent_accounts.NumberedAccount):
        raise AccountsError("accounts: caller has no numbered Claude account")
    directory = claude_account_dir(identity.label)
    prepared = prepared_account(identity.label, resume=False)
    if isinstance(prepared, AccountRefused):
        raise AccountsError(prepared.reason)
    return NamedAccount(identity.label, directory)


def account_prefix(selected: AccountSelection) -> str:
    """Return the shell assignment for an already selected account."""
    if isinstance(selected, CodexWorkersAccount):
        return f"CODEX_HOME={shlex.quote(str(selected.home))}"
    return account.named_account_prefix(selected.dir)


def trust_command(folder: Path, selected: ClaudeLaunchAccount) -> str:
    """The command the user runs to accept trust, then type /exit in Claude."""
    prefix = account_prefix(selected).strip()
    return f"cd {shlex.quote(str(folder))} && {prefix} claude"


def trust_sources(target_dir: Path, source_dir: Path | None) -> list[tuple[str, Path]]:
    """Every other account's trust file on this machine, the session's own source account first.

    `herdr.heal_claude_trust` copies a folder's trust from the first of them that has it.
    """
    home = (accounts_home() / ".claude").resolve()
    candidates = [NamedAccount(source_dir.name.removeprefix("."), source_dir)] if source_dir is not None else []
    candidates.extend(_existing_named_accounts())
    sources: dict[Path, tuple[str, Path]] = {}
    for named in candidates:
        directory = named.dir.resolve()
        if directory != target_dir.resolve() and directory not in sources:
            sources[directory] = (named.label, accounts_home() / ".claude.json" if directory == home else named.dir / ".claude.json")
    return list(sources.values())


def launch_prefix(role: str) -> str:
    """Return the shell assignment needed before launching this role."""
    return account_prefix(role_account(role))


def multiple_accounts_configured() -> bool:
    """Whether account configuration or a named account opts into role displays."""
    return bool(_existing_named_accounts())


def checked_named_accounts() -> list[NamedAccount]:
    """Every existing named account fit for a live reading or launch."""
    return [named for named in _existing_named_accounts()
            if isinstance(_inspect_account_directory(
                named, _account_note(named.label), allow_shared_repair=True,
            ), AccountDirectoryReady)]


def account_identity(config_dir: Path) -> agent_accounts.AccountIdentity:
    """Distinguish a numbered destination from an unnoted or unreadable login."""
    identity = account.claude_account(config_dir)
    if identity is None:
        return agent_accounts.UnreadableIdentity(config_dir, "the home's sign-in cannot be read")
    parsed = note_file.parse_account_label(identity.label)
    if not isinstance(parsed, note_file.NumberedAccountLabel) or parsed.tool != "claude":
        return agent_accounts.UnnotedLogin(config_dir, identity.login)
    return agent_accounts.NumberedAccount(parsed.label, config_dir, identity.login)


def home_account_label(tool: Literal["claude", "codex"]) -> str:
    """Read a home's current number, retaining its folder only for legacy display."""
    home = accounts_home() / f".{tool}"
    if tool == "claude":
        identity = account_identity(home)
        return identity.label if isinstance(identity, agent_accounts.NumberedAccount) else "~/.claude"
    return agent_accounts.codex_home_role_label(home)


def unnoted_home_line(home: Path, login: str) -> str:
    tool = "codex" if home.name.startswith(".codex") else "claude"
    return (f"~/{home.name} — no account note, not selectable: "
            f"add ~/rust/hanadocs/agents/{tool}N.md with login: {login}")


def refuse_default(label: str) -> None:
    """Reject the old alias before a destination matcher can interpret it."""
    if label != "default":
        return
    labels: list[str] = []
    for tool in ("claude", "codex"):
        label = home_account_label(tool)
        if label == f"~/.{tool}":
            home = accounts_home() / f".{tool}"
            login = (_codex_home_login(home) if tool == "codex" else
                     identity.login if isinstance(identity := account_identity(home), agent_accounts.UnnotedLogin) else "unreadable")
            labels.append(unnoted_home_line(home, login))
        else:
            labels.append(f"~/.{tool} is {label}")
    raise AccountsError("accounts: name the account by number (" + "; ".join(labels) + ")")


def numbered_account_label(label: str, tool: Literal["claude", "codex"]) -> note_file.NumberedAccountLabel:
    """Validate a supplied account number before any destination is inspected."""
    refuse_default(label)
    parsed = note_file.parse_account_label(label)
    if not isinstance(parsed, note_file.NumberedAccountLabel) or parsed.tool != tool:
        raise AccountsError(f"accounts: {label!r} is not a numbered {tool} account; use {tool}N")
    return parsed


def claude_account_dir(label: str) -> Path:
    """Resolve a numbered Claude note to the directory holding that login."""
    parsed = numbered_account_label(label, "claude")
    expected = _account_note(parsed.label)
    if not isinstance(expected, ExpectedAccountLogin):
        raise AccountsError(expected.problem)
    return account_dir(parsed.label)


def claude_workers_account(label: str) -> NamedAccount:
    """Check a queued workers destination without changing its folder."""
    directory = claude_account_dir(label)
    if not directory.exists():
        raise AccountsError(f"accounts: {label} is not set up; run accounts.py setup {label}")
    prepared = prepared_account_preview(label, resume=True)
    if isinstance(prepared, AccountRefused):
        if isinstance(account_directory_readiness(label), AccountDirectoryLoggedOut):
            raise AccountsError(f"accounts: {label} is not logged in; run CLAUDE_CONFIG_DIR={shlex.quote(str(directory))} claude and /login")
        raise AccountsError(prepared.reason)
    return NamedAccount(label, directory)


def claude_account_selection() -> agent_accounts.ClaudeAccountsInUse:
    """Freeze the default, readable directories and local live logins once per run."""
    from scripts.message import sessions

    default = account_identity(main_config_dir())
    home_state = (account.LegacyHomeLogin(account.Account("claude", default.login,
                  default.label if isinstance(default, agent_accounts.NumberedAccount) else default.login))
                  if isinstance(default, (agent_accounts.NumberedAccount, agent_accounts.UnnotedLogin))
                  else account.ConfigOnlyHome())
    named_accounts: list[agent_accounts.CheckedNamedClaudeAccount] = []
    for named in checked_named_accounts():
        identity = account_identity(named.dir)
        if isinstance(identity, agent_accounts.NumberedAccount):
            named_accounts.append(agent_accounts.CheckedNamedClaudeAccount(note_file.canonical_label(named.label), named.dir, identity))
    live_logins: set[str] = set()
    live_directories: dict[Path, None] = {}
    for session in sessions.live_sessions():
        identity = account.account_of(session["pid"], home_state=home_state)
        setting = account.process_account_directory(account.process_config_dir(session["pid"]), home_state=home_state)
        directory = setting.path if isinstance(setting, account.ConfiguredClaudeFolder) else None
        if identity is not None:
            live_logins.add(identity.login.casefold())
        if directory is not None:
            live_directories[directory] = None
    return agent_accounts.ClaudeAccountsInUse(
        default, bool(named_accounts) or len(live_logins) > 1, tuple(named_accounts),
        frozenset(live_logins), tuple(live_directories),
    )


UNIT_ROLE_GONE = "unit launches use the configured units account; select and move the group with accounts.py move units <claudeN>"


@dataclass(frozen=True)
class KeepAccount:
    label: str


@dataclass(frozen=True)
class NoKeep:
    pass


_NO_KEEP = NoKeep()
_NO_EXCLUSIONS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EarliestReset:
    at: datetime


@dataclass(frozen=True)
class PickReason:
    label: str
    weekly: ReadingWindow | NoWindow
    five_hour: ReadingWindow | NoWindow
    status: str


@dataclass(frozen=True)
class PickedAccount:
    label: str
    dir: Path
    weekly_left: float
    weekly_reset: KnownReset | NoResetKnown
    reasons: tuple[PickReason, ...] = ()


@dataclass(frozen=True)
class NoAccountWithRoom:
    earliest_reset: EarliestReset | NoResetKnown
    reasons: tuple[PickReason, ...]


def _codex_reading_can_work(reading: FreshReading, now: datetime, credits: agent_accounts.CodexCreditsSetting) -> bool:
    """Use Codex's shared allowance and credit rule through its report boundary."""
    def quota(window: ReadingWindow, label: str) -> agent_accounts.Quota:
        return agent_accounts.Quota(label=label, used_percent=100 - window.left,
            resets_at=window.reset.at if isinstance(window.reset, KnownReset) else None)
    report = agent_accounts.Report(tool="Codex", email="stored reading",
        quotas=[quota(reading.weekly, "Weekly")],
        five_hour=quota(reading.five_hour, "5-hour") if isinstance(reading.five_hour, ReadingWindow) else None,
        credit_balance=reading.credits or None)
    return isinstance(agent_accounts.codex_can_work(report, now, credits), agent_accounts.CodexWorkAvailable)


def pick(tool: Literal["claude", "codex"], now: datetime, *, exclude: frozenset[str] = _NO_EXCLUSIONS, keep: KeepAccount | NoKeep = _NO_KEEP, prefer_weekly_room: bool = False) -> PickedAccount | NoAccountWithRoom:
    """Keep a working account, otherwise spend the weekly allowance resetting soonest."""
    exclude = frozenset(numbered_account_label(label, tool).label for label in exclude)
    keep = KeepAccount(numbered_account_label(keep.label, tool).label) if isinstance(keep, KeepAccount) else keep
    candidates: list[tuple[str, Path, str]] = []
    if tool == "claude":
        directories = [(note_file.canonical_label(item.label), item.dir) for item in checked_named_accounts()]
        for label, directory in directories:
            try:
                if claude_account_dir(label) != directory:
                    continue
            except AccountsError:
                continue
            identity = account_identity(directory)
            if isinstance(identity, agent_accounts.NumberedAccount) and identity.label == label:
                candidates.append((label, directory, identity.login))
    else:
        for home in agent_accounts.codex_homes(accounts_home() / ".codex"):
            label = agent_accounts.codex_home_role_label(home)
            if not _label_is(label, "codex"):
                continue
            elif _codex_account_home(label) != home:
                # A duplicate login must not reintroduce an excluded home or change the swap destination.
                continue
            problems = _check_codex_home(label, accounts_home() / ".codex", allow_shared_repair=True)
            # Duplicate homes are one picker candidate; safety checks still reject other defects.
            if any("two homes on one login" not in problem and "keep each login in one home" not in problem for problem in problems):
                continue
            login = _codex_home_login(home)
            if login:
                candidates.append((label, home, login))
    seen: set[str] = set()
    reasons: list[PickReason] = []
    available: list[PickedAccount] = []
    allowance_available: set[str] = set()
    fallback: list[PickedAccount] = []
    any_fresh = False
    resets: list[datetime] = []
    try:
        held_refusals = refusals.load()
        refusal_problem = ""
    except refusals.RefusalsError as error:
        held_refusals = {}
        refusal_problem = str(error)
    for label, directory, login in candidates:
        excluded = (any(agent_accounts.codex_home_matches_label(directory, omitted) for omitted in exclude)
                    if tool == "codex" else label in exclude)
        if excluded or login.casefold() in seen:
            continue
        seen.add(login.casefold())
        reading = refusals.stored_reading(tool, login, now)
        any_fresh = any_fresh or isinstance(reading, FreshReading)
        weekly: ReadingWindow | NoWindow = reading.weekly if isinstance(reading, (FreshReading, StaleReading)) else NoWindow()
        five: ReadingWindow | NoWindow = reading.five_hour if isinstance(reading, (FreshReading, StaleReading)) else NoWindow()
        for window in (weekly, five):
            if isinstance(window, ReadingWindow) and isinstance(window.reset, KnownReset) and window.reset.at > now:
                resets.append(window.reset.at)
        refusal = held_refusals.get(refusals.key(tool, login))
        if refusal is not None and isinstance(refusal.reset, KnownReset) and refusal.reset.at > now:
            resets.append(refusal.reset.at)
        if refusal_problem:
            reason = refusal_problem
        elif refusal is not None and not refusals.resolved(refusal, reading, now):
            reason = "refused at " + refusal.at.astimezone(agent_accounts.user_zone()).strftime("%Y-%m-%d %H:%M %Z")
        elif tool == "claude" and isinstance(reading, FreshReading) and any(
            isinstance(window, ReadingWindow) and window.left <= 0 for window in (weekly, five)
        ):
            reason = "no room"
        elif tool == "claude":
            reason = "has room" if isinstance(reading, FreshReading) else "has room; no reading in the last 10 minutes"
            candidate = PickedAccount(label, directory, weekly.left if isinstance(weekly, ReadingWindow) and isinstance(reading, FreshReading) else 0,
                weekly.reset if isinstance(weekly, ReadingWindow) and isinstance(reading, FreshReading) else NoResetKnown())
            available.append(candidate)
            if not isinstance(reading, FreshReading):
                fallback.append(candidate)
        elif not isinstance(reading, FreshReading):
            reason = "no reading in the last 10 minutes"
            fallback.append(PickedAccount(
                label, directory, weekly.left if isinstance(weekly, ReadingWindow) else 0,
                weekly.reset if isinstance(weekly, ReadingWindow) else NoResetKnown(),
            ))
        elif tool == "codex" and not _codex_reading_can_work(reading, now, agent_accounts.codex_credits_setting(label)):
            reason = "no room"
        else:
            reason = "has room"
            available.append(PickedAccount(label, directory, reading.weekly.left, reading.weekly.reset))
            if tool == "codex" and _codex_reading_can_work(reading, now, agent_accounts.CreditsOff()):
                allowance_available.add(label)
        reasons.append(PickReason(label, weekly, five, reason))
    if not any_fresh:
        default_label = home_account_label(tool) if tool == "codex" else (fallback[0].label if fallback else "")
        preferred = keep.label if isinstance(keep, KeepAccount) else default_label
        choices = [item for label in dict.fromkeys((preferred, default_label))
                   for item in fallback if (agent_accounts.codex_home_matches_label(item.dir, label)
                                            if tool == "codex" else item.label == label)]
        if choices:
            chosen = choices[0]
            status = f"no fresh readings on this machine; using {chosen.label}"
            return replace(chosen, reasons=tuple(
                replace(reason, status=status) if reason.label == chosen.label else reason
                for reason in reasons
            ))
    if not available:
        return NoAccountWithRoom(EarliestReset(min(resets)) if resets else NoResetKnown(), tuple(reasons))
    def order(item: PickedAccount) -> tuple[bool, bool, datetime, float, str]:
        kept = isinstance(keep, KeepAccount) and (agent_accounts.codex_home_matches_label(item.dir, keep.label)
                                                 if tool == "codex" else keep.label == item.label)
        return (tool == "codex" and prefer_weekly_room and item.label not in allowance_available,
                not kept,
                item.weekly_reset.at if isinstance(item.weekly_reset, KnownReset) else datetime.max.replace(tzinfo=timezone.utc),
                -item.weekly_left, item.label)
    chosen = min(available, key=order)
    return replace(chosen, reasons=tuple(replace(reason, status="picked") if reason.label == chosen.label else reason for reason in reasons))


def codex_destination_uses_credits(chosen: PickedAccount, now: datetime) -> bool:
    """A destination needs paid credits when either included window cannot serve work."""
    for reason in chosen.reasons:
        if reason.label == chosen.label:
            if not isinstance(reason.weekly, ReadingWindow):
                return True
            return not _codex_reading_can_work(
                FreshReading(reason.weekly, reason.five_hour, "0"), now, agent_accounts.CreditsOff())
    return chosen.weekly_left <= 0



ClaudeGroup = Literal["units", "showrunners"]
CLAUDE_GROUPS: tuple[ClaudeGroup, ...] = ("units", "showrunners")


@dataclass(frozen=True)
class GroupNotConfigured:
    """No account was explicitly selected for this group."""

    @override
    def __str__(self) -> str:
        return "not configured"


GROUP_NOT_CONFIGURED = GroupNotConfigured()


@dataclass(frozen=True)
class AccountDecisionNow:
    """Use the current time for this account decision."""


ACCOUNT_DECISION_NOW = AccountDecisionNow()


class GroupAccounts(TypedDict, total=False):
    units: str
    showrunners: str


@dataclass(frozen=True)
class HandOrigin:
    """The user requested this move, independently of stored quota refusals."""


HAND_ORIGIN = HandOrigin()


@dataclass(frozen=True)
class RefusalOrigin:
    window: Literal["weekly", "five_hour", "unknown"]
    reset: KnownReset | NoResetKnown = field(default_factory=NoResetKnown)


@dataclass(frozen=True)
class AutoOrigin:
    """A configured automatic group-move rule requested this transition."""


MoveOrigin = RefusalOrigin | HandOrigin | AutoOrigin


class MoveOriginFields(TypedDict):
    cause: Literal["refusal", "by hand", "auto"]
    window: Literal["weekly", "five_hour", "unknown"]
    reset: NotRequired[str]


def move_origin_fields(origin: MoveOrigin) -> MoveOriginFields:
    fields = MoveOriginFields(cause="by hand", window="unknown")
    if isinstance(origin, AutoOrigin):
        fields["cause"] = "auto"
    if isinstance(origin, RefusalOrigin):
        fields["cause"] = "refusal"
        fields["window"] = origin.window
        if isinstance(origin.reset, KnownReset):
            fields["reset"] = origin.reset.at.isoformat()
    return fields


def move_origin_arguments(origin: MoveOrigin) -> tuple[str, ...]:
    if isinstance(origin, AutoOrigin):
        return ("--auto",)
    if isinstance(origin, HandOrigin):
        return ()
    arguments: tuple[str, ...] = ("--refused", origin.window)
    if isinstance(origin.reset, KnownReset):
        arguments += ("--reset", origin.reset.at.isoformat())
    return arguments


@dataclass(frozen=True)
class GroupTransition:
    move_id: str
    origin: MoveOrigin
    why: TransitionReason = field(default_factory=UnavailableTransitionReason)


@dataclass(frozen=True)
class NoGroupTransition:
    """A legacy selection has no recorded transition."""




@dataclass(frozen=True)
class UnitsResetSoonest:
    """Spend the allowance with the earliest weekly renewal first."""


@dataclass(frozen=True)
class ShowrunnersMostLeft:
    """Keep showrunners on the account with the most weekly allowance."""


@dataclass(frozen=True)
class GroupNoRoom:
    """Keep the group's selected account because every destination refused."""
    outcome: NoAccountWithRoom


@dataclass(frozen=True)
class GroupPlacement:
    group: ClaudeGroup
    label: str | GroupNotConfigured
    dir: Path | GroupNotConfigured
    reason: UnitsResetSoonest | ShowrunnersMostLeft | GroupNoRoom
    reasons: tuple[PickReason, ...] = ()


def groups_file() -> Path:
    return role_map.groups_file()


def _stored_groups() -> StoredGroupSelections:
    return read_role_map().groups


def group_transition(group: ClaudeGroup) -> GroupTransition | NoGroupTransition:
    selection = cast(str | StoredGroupSelection | NoGroupTransition,
                     _stored_groups().get(group, NoGroupTransition()))
    if not isinstance(selection, dict):
        return NoGroupTransition()
    origin: MoveOrigin = HandOrigin()
    if selection["cause"] == "refusal":
        reset = KnownReset(datetime.fromisoformat(selection["reset"])) if "reset" in selection else NoResetKnown()
        origin = RefusalOrigin(selection["window"], reset)
    elif selection["cause"] == "auto":
        origin = AutoOrigin()
    return GroupTransition(selection["move_id"], origin, role_map.selection_reason(selection))


def session_group(session: Session | ListedSession) -> ClaudeGroup | Literal["ungrouped"]:
    kind = session["kind"] if isinstance(session, dict) else session.kind
    if kind == "unit":
        return "units"
    if kind == "showrunner":
        return "showrunners"
    return "ungrouped"


def live_group_members(*, registrations: ShowrunnerRegistrations | Literal["read timers"] = "read timers") -> dict[ClaudeGroup, list[ListedSession]]:
    """Classify local live owners without their restart resources or worktrees."""
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    if TYPE_CHECKING:
        from scripts.shutdown import inventory
    else:
        inventory = import_module("inventory")
    members: dict[ClaudeGroup, list[ListedSession]] = {"units": [], "showrunners": []}
    for session in inventory.listing(registrations=registrations):
        group = session_group(session)
        if group != "ungrouped":
            members[group].append(session)
    return members


def session_account_label(session: ListedSession) -> str:
    return account.label_for("Claude", session.login)


def account_labels_for_logins(logins: frozenset[str]) -> frozenset[str]:
    """Exclude the numbered accounts matching these live logins."""
    labels: set[str] = set()
    for directory in (item.dir for item in checked_named_accounts()):
        identity = account_identity(directory)
        if isinstance(identity, agent_accounts.NumberedAccount) and identity.login.casefold() in logins:
            labels.add(identity.label)
    return frozenset(labels)


def placement(group: ClaudeGroup, now: datetime, *, exclude: frozenset[str] = _NO_EXCLUSIONS) -> GroupPlacement:
    """Rank Claude accounts with room for a group using the picker's reading rules."""
    chosen = pick("claude", now, exclude=exclude, keep=NoKeep())
    if isinstance(chosen, NoAccountWithRoom):
        label = group_label(group_accounts(), group)
        directory = claude_account_dir(label) if isinstance(label, str) and _label_is(label, "claude") else GROUP_NOT_CONFIGURED
        return GroupPlacement(group, label, directory, GroupNoRoom(chosen), chosen.reasons)
    eligible: list[PickedAccount] = []
    for reason in chosen.reasons:
        if not reason.status.startswith("has room") and reason.status != "picked" and not reason.status.startswith("no fresh readings"):
            continue
        directory = claude_account_dir(reason.label)
        identity = account_identity(directory)
        if not isinstance(identity, agent_accounts.NumberedAccount):
            continue
        reading = refusals.stored_reading("claude", identity.login, now)
        weekly = reading.weekly if isinstance(reading, FreshReading) else NoWindow()
        eligible.append(PickedAccount(reason.label, directory, weekly.left if isinstance(weekly, ReadingWindow) else 0,
            weekly.reset if isinstance(weekly, ReadingWindow) else NoResetKnown()))
    if not eligible:
        eligible = [chosen]
    def order(candidate: PickedAccount) -> tuple[datetime, float, str]:
        reset = candidate.weekly_reset.at if isinstance(candidate.weekly_reset, KnownReset) else datetime.max.replace(tzinfo=timezone.utc)
        return (reset, -candidate.weekly_left, f"{_label_number(candidate.label):020d}")
    selected = min(eligible, key=order) if group == "units" else min(eligible, key=lambda item: (-item.weekly_left, *order(item)))
    reason = UnitsResetSoonest() if group == "units" else ShowrunnersMostLeft()
    return GroupPlacement(group, selected.label, selected.dir, reason, chosen.reasons)


def group_label(groups: GroupAccounts, group: ClaudeGroup) -> str | GroupNotConfigured:
    """Convert the optional JSON key into its configuration meaning."""
    label = groups.get(group, GROUP_NOT_CONFIGURED)
    return note_file.canonical_label(label) if isinstance(label, str) else label


def group_configuration_line(group: ClaudeGroup) -> str:
    return f"{group}: not configured — accounts.py move {group} <claudeN>"


def group_status_line(groups: GroupAccounts) -> str:
    return f"units: {group_label(groups, 'units')}, showrunners: {group_label(groups, 'showrunners')}"



def group_note_labels() -> dict[ClaudeGroup, str]:
    """The note labels serving each configured Claude launch group."""
    groups = group_accounts()
    labels: dict[ClaudeGroup, str] = {}
    for group in CLAUDE_GROUPS:
        selected = group_label(groups, group)
        if isinstance(selected, GroupNotConfigured):
            continue
        if _label_is(selected, "claude"):
            labels[group] = selected
    return labels


def group_accounts() -> GroupAccounts:
    """Read only explicit group selections; an absent key stays unconfigured."""
    result = GroupAccounts()
    stored = _stored_groups()
    for group in CLAUDE_GROUPS:
        selection = cast(str | StoredGroupSelection | GroupNotConfigured, stored.get(group, GROUP_NOT_CONFIGURED))
        if isinstance(selection, GroupNotConfigured):
            continue
        label = selection["label"] if isinstance(selection, dict) else selection
        stamp = ""
        login = ""
        evidence_label = label
        if label in ("default", "~/.claude"):
            try:
                for line in roles_history_file().read_text(encoding="utf-8").splitlines():
                    decoded = cast(object, json.loads(line))
                    if not isinstance(decoded, dict):
                        continue
                    row = cast(dict[str, object], decoded)
                    if row.get("group") != group and not (group == "units" and row.get("role") == "unit"):
                        continue
                    # Only the last assignment is in force; an earlier home
                    # assignment cannot name a later, unrelated selection.
                    stamp = ""
                    login = ""
                    evidence_label = label
                    if row.get("label") != label:
                        continue
                    at, held_login = row.get("at"), row.get("login")
                    if isinstance(at, str):
                        stamp = at
                    if isinstance(held_login, str):
                        login = held_login
                    recorded_label = row.get("account_label")
                    if isinstance(recorded_label, str) and _label_is(recorded_label, "claude"):
                        evidence_label = recorded_label
            except (OSError, ValueError):
                pass
        resolved = resolve_history_label(evidence_label, stamp, login=login)
        result[group] = resolved.label if isinstance(resolved, ResolvedHistoryLabel) else "home account unresolved"
    return result


def set_group_account(group: ClaudeGroup, label: str, *, why: str,
                      now: datetime | AccountDecisionNow = ACCOUNT_DECISION_NOW,
                      origin: MoveOrigin = HAND_ORIGIN, move_id: str = "") -> GroupTransition:
    from uuid import uuid4

    label = numbered_account_label(label, "claude").label
    if role_map.is_role_map_host():
        _ = claude_account_dir(label)
    at = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    fields = move_origin_fields(origin)
    selection = StoredGroupSelection(label=label, move_id=move_id or uuid4().hex,
                                     cause=fields["cause"], window=fields["window"])
    if "reset" in fields:
        selection["reset"] = fields["reset"]
    write_role_map(RoleMapChange(group, label, why, at.isoformat(), selection["move_id"],
        fields["cause"], fields["window"], fields.get("reset", "")))
    return GroupTransition(selection["move_id"], origin, RecordedTransitionReason(why))


def placement_report(now: datetime | AccountDecisionNow = ACCOUNT_DECISION_NOW) -> str:
    at = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    groups = group_accounts()
    room = pick("claude", at, keep=NoKeep())
    lines = [group_status_line(groups)]
    lines.extend(_pick_explanation(reason) for reason in room.reasons)
    for group in CLAUDE_GROUPS:
        configured = group_label(groups, group)
        if isinstance(configured, GroupNotConfigured):
            lines.append(group_configuration_line(group))
            continue
        if not _label_is(configured, "claude"):
            continue
        excluded = frozenset({configured})
        directory = claude_account_dir(configured)
        identity = account_identity(directory)
        if isinstance(identity, agent_accounts.NumberedAccount):
            excluded |= account_labels_for_logins(frozenset({identity.login.casefold()}))
        chosen = placement(group, at, exclude=excluded)
        reason_text = (no_room_line("claude", chosen.reason.outcome) if isinstance(chosen.reason, GroupNoRoom) else
                       "soonest weekly reset" if isinstance(chosen.reason, UnitsResetSoonest) else "most weekly left")
        lines.append(f"{group} on refusal: {chosen.label}; {reason_text}")
    return "\n".join(lines)


def no_room_line(tool: str, outcome: NoAccountWithRoom) -> str:
    reset = outcome.earliest_reset
    time = reset.at.astimezone(agent_accounts.user_zone()).strftime("%a %Y-%m-%d %H:%M %Z") if isinstance(reset, EarliestReset) else "unknown"
    return f"no {tool} account has room; the earliest reset is {time}"


def _pick_explanation(reason: PickReason) -> str:
    weekly = f"{reason.weekly.left:g}%" if isinstance(reason.weekly, ReadingWindow) else "unavailable"
    reset = (reason.weekly.reset.at.astimezone(agent_accounts.user_zone()).strftime("%a %Y-%m-%d %H:%M %Z")
             if isinstance(reason.weekly, ReadingWindow) and isinstance(reason.weekly.reset, KnownReset) else "unknown")
    five = f"{reason.five_hour.left:g}%" if isinstance(reason.five_hour, ReadingWindow) else "no window"
    return f"{reason.label}: weekly {weekly}, resets {reset}, 5-hour {five}; {reason.status}"


def readings_file() -> Path:
    """The quota readings log, resolved from this call's HOME."""
    return accounts_home() / ".local" / "state" / "agent-notes" / "readings.jsonl"


def _iso_time(text: str, place: str) -> datetime:
    try:
        at = datetime.fromisoformat(text)
    except ValueError as error:
        raise AccountsError(f"accounts: {place} must be an ISO timestamp") from error
    if at.tzinfo is None:
        # Command-line and historical wall times use the user's zone.
        from scripts.lib.user_zone import user_zone

        at = at.replace(tzinfo=user_zone())
    return at.astimezone(timezone.utc)


def _quota_observations() -> dict[str, list[WeeklyQuotaObservation]]:
    path = readings_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as error:
        raise AccountsError(f"accounts: could not read {path}: {error}") from error
    observations: dict[str, dict[datetime, WeeklyQuotaObservation]] = {}
    for line in lines:
        try:
            decoded = cast(object, json.loads(line))
            if not isinstance(decoded, dict):
                continue
            values = cast(dict[str, object], decoded)
            label, stamp, left = values.get("account"), values.get("at"), values.get("remaining")
            if (
                not isinstance(label, str) or not isinstance(stamp, str)
                or isinstance(left, bool) or not isinstance(left, (int, float))
                or not math.isfinite(left) or not 0 <= left <= 100
            ):
                continue
            login = values.get("login", "")
            resolved = resolve_history_label(label, stamp, login=login if isinstance(login, str) else "")
            label = resolved.label if isinstance(resolved, ResolvedHistoryLabel) else "home account unresolved"
            wire: QuotaReading = {"account": label, "at": stamp, "remaining": float(left)}
            at = datetime.fromisoformat(wire["at"])
            if at.tzinfo is None:
                continue
            at = at.astimezone(timezone.utc)
            observations.setdefault(label, {})[at] = WeeklyQuotaObservation(at, wire["remaining"])
        except (ValueError, TypeError):
            continue
    return {
        label: sorted(found.values(), key=lambda observation: observation.at)
        for label, found in observations.items()
    }


def resolve_history_label(label: str, at: str, *, login: str = "") -> ResolvedHistoryLabel | HistoricalAccountUnresolved:
    """Resolve a historical home from its own login or a dated cutover only."""
    parsed = note_file.parse_account_label(label)
    if isinstance(parsed, note_file.NumberedAccountLabel):
        return ResolvedHistoryLabel(parsed.label)
    tool: Literal["claude", "codex"] = "codex" if label in ("~/.codex", str(accounts_home() / ".codex")) else "claude"
    if label not in ("default", f"~/.{tool}", str(accounts_home() / f".{tool}")):
        return HistoricalAccountUnresolved()
    if login:
        parsed = note_file.parse_account_label(account.label_for(tool, login))
        if isinstance(parsed, note_file.NumberedAccountLabel) and parsed.tool == tool:
            return ResolvedHistoryLabel(parsed.label)
    try:
        rows = _read_json_object(accounts_config_file()).get("home_cutovers", [])
        stamp = datetime.fromisoformat(at)
        if stamp.tzinfo is None or not isinstance(rows, list):
            return HistoricalAccountUnresolved()
        evidence: list[tuple[datetime, str]] = []
        for item in cast(list[object], rows):
            if not isinstance(item, dict):
                continue
            row = cast(dict[str, object], item)
            boundary, number = row.get("at"), row.get("label")
            if not isinstance(boundary, str) or not isinstance(number, str):
                continue
            end = datetime.fromisoformat(boundary)
            parsed = note_file.parse_account_label(number)
            if (end.tzinfo is not None and stamp < end
                    and (not login or row.get("login") == login)
                    and isinstance(parsed, note_file.NumberedAccountLabel) and parsed.tool == tool):
                evidence.append((end, parsed.label))
        if evidence:
            return ResolvedHistoryLabel(min(evidence)[1])
    except (OSError, ValueError, TypeError):
        pass
    return HistoricalAccountUnresolved()


def _unit_role_history() -> list[UnitRoleAssignment]:
    path = roles_history_file()
    try:
        lines = worker_names.role_history_lines(path)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as error:
        raise AccountsError(f"accounts: could not read {path}: {error}") from error
    history: list[UnitRoleAssignment] = []
    for index, line in enumerate(lines, 1):
        try:
            decoded = cast(object, json.loads(line))
            if not isinstance(decoded, dict):
                raise ValueError("expected a JSON object")
            values = cast(dict[str, object], decoded)
            if values.get("group") in ("units", "showrunners"):
                continue
            role = values.get("role")
            if role not in {"unit", "workers"}:
                raise ValueError("invalid role")
            label, stamp = values.get("label"), values.get("at")
            if not isinstance(label, str) or not isinstance(stamp, str):
                raise ValueError("invalid label or timestamp")
            _ = _iso_time(stamp, "role time")
            if role == "workers":
                continue
            evidence = values.get("account_label", label)
            if not isinstance(evidence, str):
                raise ValueError("invalid account_label")
            login = values.get("login", "")
            resolved = resolve_history_label(evidence, stamp, login=login if isinstance(login, str) else "")
            account_label = resolved.label if isinstance(resolved, ResolvedHistoryLabel) else "home account unresolved"
            wire = LegacyRoleChange(at=stamp, role="unit", label=label)
            history.append(UnitRoleAssignment(_iso_time(wire["at"], "role time"), note_file.canonical_label(account_label)))
        except (ValueError, TypeError) as error:
            raise AccountsError(f"accounts: {path}:{index}: invalid role change: {error}") from error
    return sorted(history, key=lambda assignment: assignment.at)


# Fixed account roles ended when account selection moved to available room.
UNIT_ROLE_ENDED_AT = datetime(2026, 10, 13, tzinfo=timezone.utc)


def _historical_roles(
    label: str, at: datetime, history: list[UnitRoleAssignment],
) -> tuple[agent_accounts.ClaudeRole, ...]:
    if at >= UNIT_ROLE_ENDED_AT:
        return ()
    home = resolve_history_label("default", at.isoformat())
    default_label = home.label if isinstance(home, ResolvedHistoryLabel) else ""
    unit_label = default_label
    for assignment in history:
        if assignment.at > at:
            break
        unit_label = assignment.label
    roles: list[agent_accounts.ClaudeRole] = []
    if label == default_label:
        roles.append("showrunners")
    if label == unit_label:
        roles.append("unit directors")
    return tuple(roles)


def burn(since: datetime, until: datetime) -> list[AccountRoleBurn]:
    """Spend per observed day, split at refills, window edges, and role changes.

    Deltas between observations are spread uniformly within their interval.
    A rise in remaining allowance starts a new span: no spend or observed time
    is inferred across that refill. One observation alone gives no rate.
    """
    if since.tzinfo is None or until.tzinfo is None or until <= since:
        raise AccountsError("accounts: burn requires an increasing, timezone-aware window")
    history = _unit_role_history()
    totals: dict[tuple[str, tuple[agent_accounts.ClaudeRole, ...]], tuple[float, float]] = {}
    for label, observations in _quota_observations().items():
        for before, after in zip(observations, observations[1:]):
            start, end = max(before.at, since), min(after.at, until)
            if start >= end or after.remaining > before.remaining:
                continue
            seconds = (after.at - before.at).total_seconds()
            rate = (before.remaining - after.remaining) / seconds
            boundaries = [start, *(assignment.at for assignment in history if start < assignment.at < end), end]
            if start < UNIT_ROLE_ENDED_AT < end:
                boundaries.append(UNIT_ROLE_ENDED_AT)
            boundaries.sort()
            for left, right in zip(boundaries, boundaries[1:]):
                span = (right - left).total_seconds()
                if span <= 0:
                    continue
                roles = _historical_roles(label, left, history)
                key = (label, roles)
                points, observed_seconds = totals.get(key, (0.0, 0.0))
                totals[key] = (points + rate * span, observed_seconds + span)
    return [
        AccountRoleBurn(label, roles, points, seconds / 86400)
        for (label, roles), (points, seconds) in sorted(totals.items())
    ]


def _atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            _ = output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


class HomeCutoverEvidence(TypedDict):
    label: str
    login: str
    at: str


class HomeCutover(HomeCutoverEvidence):
    state: Literal["moving", "complete", "undoing", "undone"]


class MacHomeCutover(HomeCutover):
    platform: Literal["darwin"]
    oauthAccount: dict[str, object]


def _record_home_cutover(journal: HomeCutover) -> None:
    with _roles_lock():
        configuration = _read_json_object(accounts_config_file()) if accounts_config_file().exists() else {}
        rows = configuration.get("home_cutovers", [])
        if not isinstance(rows, list):
            raise AccountsError("accounts: invalid home_cutovers history")
        history = cast(list[object], rows)
        evidence = HomeCutoverEvidence(label=journal["label"], login=journal["login"], at=journal["at"])
        if evidence not in history:
            history.append(evidence)
        configuration["home_cutovers"] = history
        _atomic_write(accounts_config_file(), json.dumps(configuration) + "\n")


def _mac_process_arguments(pid: int) -> list[str]:
    """Read the kernel's NUL-separated argv, preserving spaces and prompt quotes."""
    record = account.mac_process_record(pid)
    if isinstance(record, account.ProcessEnvironmentUnread):
        raise AccountsError(f"accounts: cannot read process {pid} arguments; no identity moved")
    return list(record.arguments)


def _claude_process_ids() -> list[int]:
    """Every Claude Code process owned by this user, including socketless ones."""
    try:
        listing = subprocess.run(
            ("ps", "-u", str(os.getuid()), "-o", "pid=,comm=,args="),
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AccountsError("accounts: cannot list Claude processes; no identity moved") from error
    found: list[int] = []
    for line in listing.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) < 2 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        command = Path(fields[1]).name
        # ps flattens argv without quoting. Never shell-parse user prompt text.
        arguments = fields[2].split() if len(fields) == 3 else []
        runtime = command in ("node", "nodejs", "bun")
        if not (command == "claude" or runtime
                or arguments and Path(arguments[0]).name == "claude"):
            continue
        if sys.platform == "linux":
            try:
                raw_arguments = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError as error:
                raise AccountsError(f"accounts: cannot read process {pid} arguments; no identity moved") from error
            if not raw_arguments.endswith(b"\0") or not raw_arguments.removesuffix(b"\0"):
                raise AccountsError(f"accounts: cannot read process {pid} arguments; no identity moved")
            arguments = [os.fsdecode(word) for word in raw_arguments.removesuffix(b"\0").split(b"\0")]
        elif sys.platform == "darwin":
            arguments = _mac_process_arguments(pid)
        if (command == "claude"
                or arguments and Path(arguments[0]).name == "claude"
                or runtime and any(
                    word.endswith("/@anthropic-ai/claude-code/cli.js") for word in arguments)):
            found.append(pid)
    return found


def _cutover_session_check(folder: Path) -> None:
    for pid in _claude_process_ids():
        setting = account.process_config_dir(pid)
        if isinstance(setting, account.ProcessEnvironmentUnread):
            raise AccountsError("accounts: cannot establish the account of a live session")
        selected = setting.path if isinstance(setting, account.ConfiguredClaudeFolder) else main_config_dir()
        if selected.expanduser().resolve() == folder.resolve():
            raise AccountsError(f"accounts: a live session uses {folder}; stop it before cutover-home")


MAC_HOME_LOGIN_STEP = "Mac's own step: one /login in ~/.claude2, never /logout"


def _mac_login_keychain_visible() -> bool:
    try:
        result = subprocess.run(
            ("security", "list-keychains"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15,
        )
        paths = shlex.split(result.stdout)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as error:
        raise AccountsError("accounts: cannot read the keychain search list") from error
    if result.returncode:
        raise AccountsError("accounts: cannot read the keychain search list")
    keychains = accounts_home() / "Library/Keychains"
    return any(str(keychains / name) in paths for name in ("login.keychain-db", "login.keychain"))


def _mac_keychain_has_entry(folder: Path) -> bool:
    try:
        result = subprocess.run(
            ("security", "find-generic-password", "-s", agent_accounts.claude_keychain_service(folder)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AccountsError("accounts: cannot check the keychain entry") from error
    if result.returncode == 44:
        return False
    if result.returncode:
        raise AccountsError("accounts: cannot check the keychain entry")
    return True


def _delete_legacy_keychain_entry() -> None:
    try:
        result = subprocess.run(
            ("security", "delete-generic-password", "-s", agent_accounts.claude_keychain_service(main_config_dir())),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AccountsError("accounts: cannot delete the legacy keychain entry; rerun cutover-home") from error
    # errSecItemNotFound (44) also finishes a deletion interrupted before journalling.
    if result.returncode not in (0, 44):
        raise AccountsError("accounts: cannot delete the legacy keychain entry; rerun cutover-home")


def _mac_oauth_account(values: dict[str, object]) -> dict[str, object]:
    raw = values.get("oauthAccount")
    if not isinstance(raw, dict):
        raise AccountsError("accounts: cutover requires an oauthAccount in both homes")
    identity = cast(dict[str, object], raw)
    identifier = identity.get("accountUuid")
    if not isinstance(identifier, str) or not identifier:
        raise AccountsError("accounts: cutover cannot establish the account identifier")
    return identity


def _cutover_home_macos(label: str, *, undo: bool) -> None:
    """Retire the legacy login only after the user's numbered login is verified."""
    path = accounts_home() / ".local/state/accounts/cutover-home.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.is_symlink():
            raise AccountsError("accounts: cutover journal must not be a link")
        raw = _read_json_object(path) if path.exists() else {}
        if raw and raw.get("platform") != "darwin":
            raise AccountsError("accounts: finish the recorded cutover on its original platform")
        if raw:
            if (not all(isinstance(raw.get(key), str) and raw[key] for key in ("label", "login", "at"))
                    or raw.get("state") not in ("moving", "complete", "undoing", "undone")):
                raise AccountsError("accounts: cutover journal is unreadable; no identity changed")
            _ = _mac_oauth_account(raw)
            recorded = cast(MacHomeCutover, cast(object, raw))
            if label and numbered_account_label(label, "claude").label != recorded["label"]:
                raise AccountsError("accounts: finish or undo the recorded cutover first")
            label = recorded["label"]
            if not undo and recorded["state"] == "undone":
                raw = {}
        else:
            if undo:
                raise AccountsError("accounts: no cutover to undo")
            label = numbered_account_label(label, "claude").label
        destination = account_dir(label)
        _cutover_session_check(destination if undo else main_config_dir())
        seed_path = accounts_home() / ".claude.json"
        numbered_path = destination / ".claude.json"
        if any(item.is_symlink() for item in (main_config_dir(), destination, seed_path, numbered_path)):
            raise AccountsError("accounts: cutover homes and identity files must not be links")
        seed = _read_json_object(seed_path)
        if raw:
            journal = cast(MacHomeCutover, cast(object, raw))
        else:
            source = _mac_oauth_account(seed)
            source_login = source.get("emailAddress")
            if not isinstance(source_login, str) or not source_login:
                raise AccountsError("accounts: cutover-home requires a readable home login")
            journal = MacHomeCutover(label=label, login=source_login, at=datetime.now(timezone.utc).isoformat(),
                                     state="moving", platform="darwin", oauthAccount=source)
        if undo:
            if journal["state"] != "undone":
                if "oauthAccount" in seed and seed["oauthAccount"] != journal["oauthAccount"]:
                    raise AccountsError("accounts: ~/.claude already holds another login")
                journal["state"] = "undoing"
                _atomic_write(path, json.dumps(journal) + "\n")
                seed["oauthAccount"] = journal["oauthAccount"]
                _atomic_write(seed_path, json.dumps(seed) + "\n")
                journal["state"] = "undone"
                _atomic_write(path, json.dumps(journal) + "\n")
            print("accounts: restored home identity; the keychain entry needs one /login in ~/.claude (never /logout)")
            return
        if journal["state"] == "complete":
            return
        if journal["state"] == "undoing":
            raise AccountsError("accounts: finish undo before starting again")
        expected = _account_note(journal["label"])
        numbered = _read_json_object(numbered_path) if numbered_path.exists() else {}
        numbered_oauth = _mac_oauth_account(numbered)
        source_oauth = journal["oauthAccount"]
        if source_oauth["accountUuid"] != numbered_oauth["accountUuid"]:
            raise AccountsError("accounts: the numbered login is not the same account as the legacy login")
        login = source_oauth.get("emailAddress")
        if (not isinstance(expected, ExpectedAccountLogin) or not isinstance(login, str)
                or expected.login.casefold() != login.casefold()):
            raise AccountsError("accounts: cutover-home requires a matching account note and home login")
        if not _mac_login_keychain_visible():
            raise AccountsError("accounts: this session cannot see the Mac's login keychain; "
                                + "run cutover-home in a Terminal on the Mac")
        if not _mac_keychain_has_entry(destination):
            raise AccountsError("accounts: the numbered keychain entry is missing; complete its /login first")
        if not raw:
            _atomic_write(path, json.dumps(journal) + "\n")
        if "oauthAccount" in seed:
            if _mac_oauth_account(seed) != source_oauth:
                raise AccountsError("accounts: ~/.claude now holds another login")
            del seed["oauthAccount"]
            _atomic_write(seed_path, json.dumps(seed) + "\n")
        if _mac_keychain_has_entry(main_config_dir()):
            _delete_legacy_keychain_entry()
        _record_home_cutover(journal)
        journal["state"] = "complete"
        _atomic_write(path, json.dumps(journal) + "\n")


@dataclass(frozen=True)
class CodexHomeProcess:
    pid: int
    command: str
    setting: account.ProcessCodexHome | account.ProcessWithoutCodexHome | account.ProcessEnvironmentUnread


def list_codex_home_processes() -> list[CodexHomeProcess]:
    """Keep Codex argv and home selection, excluding environment text from diagnostics."""
    command = (("ps", "-u", str(os.getuid()), "-o", "pid=,ucomm=")
               if sys.platform == "darwin" else
               ("ps", "-u", str(os.getuid()), "-o", "pid=,comm=,args="))
    try:
        listing = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise AccountsError("accounts: cannot list Codex processes; no login moved") from error
    found: list[CodexHomeProcess] = []
    for line in listing.stdout.splitlines():
        fields = line.split(maxsplit=1 if sys.platform == "darwin" else 2)
        if len(fields) < 2 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        executable = fields[1].strip()
        if (executable if sys.platform == "darwin" else Path(executable).name) != "codex":
            continue
        setting: account.ProcessCodexHome | account.ProcessWithoutCodexHome | account.ProcessEnvironmentUnread
        if sys.platform == "darwin":
            record = account.mac_process_record(pid)
            setting = account.process_codex_home(pid)
            if isinstance(record, account.MacProcessRecord):
                process_command = shlex.join(record.arguments)
            else:
                process_command = "codex"
                setting = record
        else:
            process_command = fields[2] if len(fields) == 3 else executable
            setting = account.process_codex_home(pid)
        found.append(CodexHomeProcess(pid, process_command, setting))
    return found


def _codex_cutover_process_check(folder: Path) -> None:
    problems: list[str] = []
    for process in list_codex_home_processes():
        setting = process.setting
        if isinstance(setting, account.ProcessEnvironmentUnread):
            problems.append(f"cannot read environment of process {process.pid} ({process.command})")
            continue
        selected = (setting.home if isinstance(setting, account.ProcessCodexHome)
                    else accounts_home() / ".codex")
        if selected.expanduser().resolve() == folder.resolve():
            problems.append(f"process {process.pid} ({process.command}) uses {folder}")
    if problems:
        raise AccountsError("accounts: " + "; ".join(problems) + "; stop it, then rerun cutover-home")


def cutover_codex_home(label: str, *, undo: bool = False) -> None:
    """Rename only auth.json; durable intent lets either direction resume safely."""
    label = numbered_account_label(label, "codex").label
    source = accounts_home() / ".codex"
    destination = codex_setup_home(label)
    path = accounts_home() / ".local/state/accounts/cutover-codex-home.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            raw = _read_json_object(path)
        except FileNotFoundError:
            raw = {}
        if raw and not undo and raw.get("state") in ("complete", "undone") and _codex_home_login(source):
            raw = {}
        if raw:
            if (not all(isinstance(raw.get(key), str) and raw[key] for key in ("label", "login", "at"))
                    or raw.get("state") not in ("moving", "complete", "undoing", "undone")
                    or not _label_is(str(raw.get("label")), "codex")):
                raise AccountsError("accounts: Codex cutover journal is unreadable; no login moved")
            if raw["label"] != label:
                raise AccountsError("accounts: finish or undo the recorded cutover first")
            if raw["state"] == ("undone" if undo else "complete"):
                return
            if not undo and raw["state"] == "undone":
                raw = {}
        home_auth, numbered_auth = source / "auth.json", destination / "auth.json"
        _codex_cutover_process_check(destination if undo else source)
        if source.is_symlink() or destination.is_symlink():
            raise AccountsError("accounts: cutover homes must be real directories")
        if not source.is_dir() or destination.exists() and not destination.is_dir():
            raise AccountsError("accounts: cutover homes must be real directories")
        if home_auth.is_symlink() or numbered_auth.is_symlink():
            raise AccountsError("accounts: cutover identity files must not be links")
        if any(path.exists() and not path.is_file() for path in (home_auth, numbered_auth)):
            raise AccountsError("accounts: cutover identity files must be regular files")
        if home_auth.exists() and numbered_auth.exists():
            raise AccountsError(f"accounts: {source if undo else destination} already holds auth.json")
        if not raw:
            if undo:
                raise AccountsError("accounts: no Codex cutover to undo")
            expected = _account_note(label)
            login = agent_accounts.codex_email_on_disk(source)
            if not isinstance(expected, ExpectedAccountLogin) or not login:
                raise AccountsError("accounts: cutover-home requires a readable account note and home login")
            if expected.login.casefold() != login.casefold():
                raise AccountsError(f"accounts: ~/.codex does not hold {label}'s login")
            if numbered_auth.exists():
                raise AccountsError(f"accounts: {destination} already holds auth.json")
            journal = HomeCutover(label=label, login=login,
                                  at=datetime.now(timezone.utc).isoformat(), state="moving")
        else:
            journal = cast(HomeCutover, cast(object, raw))
        if undo:
            journal["state"] = "undoing"
            _atomic_write(path, json.dumps(journal) + "\n")
            if numbered_auth.exists():
                os.replace(numbered_auth, home_auth)
            elif not home_auth.exists():
                raise AccountsError("accounts: neither Codex home holds auth.json; no login moved")
            journal["state"] = "undone"
            _atomic_write(path, json.dumps(journal) + "\n")
            return
        if journal["state"] == "undoing":
            raise AccountsError("accounts: finish undo before starting the Codex cutover again")
        if raw and journal["state"] == "moving" and home_auth.exists():
            login = agent_accounts.codex_email_on_disk(source)
            if not login or login.casefold() != journal["login"].casefold():
                raise AccountsError("accounts: ~/.codex now holds another login than the recorded cutover; undo it first")
        if not home_auth.exists() and not numbered_auth.exists():
            raise AccountsError("accounts: neither Codex home holds auth.json; no login moved")
        if not raw:
            _atomic_write(path, json.dumps(journal) + "\n")
        problems = _link_codex_home(label)
        if problems:
            raise AccountsError("; ".join(problems))
        if home_auth.exists():
            os.replace(home_auth, numbered_auth)
        _record_home_cutover(journal)
        journal["state"] = "complete"
        _atomic_write(path, json.dumps(journal) + "\n")
        prepared = _prepare_codex_home(label)
        if isinstance(prepared, agent_accounts.CodexHomePreparationFailed):
            raise AccountsError(
                f"accounts: {_codex_folder(destination)} login moved, but its shared session index is not ready "
                + f"({prepared.problem}); run accounts.py setup {label} to finish the index"
            )
        print(f"{label} ({_codex_folder(destination)}) login moved; shared session index ready.")


def cutover_home(label: str = "", *, undo: bool = False) -> None:
    """Move the legacy home identity, retaining a recoverable intent before mutation.

    Credentials are only renamed. The identity block moves between atomic JSON
    replacements; the journal lets either direction resume after any replacement.
    """
    if _label_is(label, "codex"):
        cutover_codex_home(label, undo=undo)
        return
    if sys.platform == "darwin":
        _cutover_home_macos(label, undo=undo)
        return
    path = accounts_home() / ".local/state/accounts/cutover-home.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            raw = _read_json_object(path)
        except FileNotFoundError:
            raw = {}
        if raw and not undo and raw.get("state") in ("complete", "undone"):
            identity = _directory_identity(main_config_dir())
            if isinstance(identity, ClaudeDirectoryLogin):
                raw = {}
        if raw:
            if (not all(isinstance(raw.get(key), str) and raw[key] for key in ("label", "login", "at"))
                    or raw.get("state") not in ("moving", "complete", "undoing", "undone")):
                raise AccountsError("accounts: cutover journal is unreadable; no identity moved")
            journal = cast(HomeCutover, cast(object, raw))
            if label and numbered_account_label(label, "claude").label != journal["label"]:
                raise AccountsError("accounts: finish or undo the recorded cutover first")
        else:
            if undo:
                raise AccountsError("accounts: no cutover to undo")
            label = numbered_account_label(label, "claude").label
            expected = _account_note(label)
            identity = _directory_identity(main_config_dir())
            if not isinstance(expected, ExpectedAccountLogin) or not isinstance(identity, ClaudeDirectoryLogin):
                raise AccountsError("accounts: cutover-home requires a readable account note and home login")
            if expected.login.casefold() != identity.login.casefold():
                raise AccountsError(f"accounts: ~/.claude does not hold {label}'s login")
            journal = HomeCutover(label=label, login=identity.login,
                                  at=datetime.now(timezone.utc).isoformat(), state="moving")
        destination = account_dir(journal["label"])
        _cutover_session_check(destination if undo else main_config_dir())
        if destination.is_symlink() or main_config_dir().is_symlink():
            raise AccountsError("accounts: cutover homes must be real directories")
        seed_path = accounts_home() / ".claude.json"
        numbered_path = destination / ".claude.json"
        seed = _read_json_object(seed_path)
        numbered = _read_json_object(numbered_path) if numbered_path.exists() else {}
        home_credentials = main_config_dir() / ".credentials.json"
        numbered_credentials = destination / ".credentials.json"
        if any(item.is_symlink() for item in (seed_path, numbered_path, home_credentials, numbered_credentials)):
            raise AccountsError("accounts: cutover identity files must not be links")
        if not raw:
            if "oauthAccount" in numbered or numbered_credentials.exists():
                raise AccountsError(f"accounts: {destination} already holds a login or oauthAccount")
            _atomic_write(path, json.dumps(journal) + "\n")
        if undo:
            if journal["state"] == "undone":
                return
            home_identity = _directory_identity(main_config_dir())
            if (isinstance(home_identity, ClaudeDirectoryLogin)
                    and home_identity.login.casefold() != journal["login"].casefold()):
                raise AccountsError("accounts: ~/.claude already holds another login")
            if ("oauthAccount" in seed and "oauthAccount" in numbered
                    and seed["oauthAccount"] != numbered["oauthAccount"]):
                raise AccountsError("accounts: ~/.claude already holds another login")
            if home_credentials.exists() and numbered_credentials.exists():
                raise AccountsError("accounts: both homes hold credentials; refusing to overwrite")
            journal["state"] = "undoing"
            _atomic_write(path, json.dumps(journal) + "\n")
            if "oauthAccount" in numbered:
                seed["oauthAccount"] = numbered["oauthAccount"]
                _atomic_write(seed_path, json.dumps(seed) + "\n")
                del numbered["oauthAccount"]
                _atomic_write(numbered_path, json.dumps(numbered) + "\n")
            if numbered_credentials.exists():
                _ = numbered_credentials.rename(home_credentials)
            journal["state"] = "undone"
            _atomic_write(path, json.dumps(journal) + "\n")
            return
        if journal["state"] == "complete":
            return
        if journal["state"] in ("undone", "undoing"):
            raise AccountsError("accounts: cutover was undone; finish undo before starting again")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        if "oauthAccount" in seed:
            if "oauthAccount" in numbered and numbered["oauthAccount"] != seed["oauthAccount"]:
                raise AccountsError("accounts: destination holds another oauthAccount")
            numbered = {**_filtered_seed(seed), **numbered, "oauthAccount": seed["oauthAccount"]}
            _atomic_write(numbered_path, json.dumps(numbered) + "\n")
            del seed["oauthAccount"]
            _atomic_write(seed_path, json.dumps(seed) + "\n")
        if home_credentials.exists():
            if numbered_credentials.exists():
                raise AccountsError("accounts: destination already holds credentials")
            _ = home_credentials.rename(numbered_credentials)
        _record_home_cutover(journal)
        journal["state"] = "complete"
        _atomic_write(path, json.dumps(journal) + "\n")


@contextmanager
def _roles_lock() -> Generator[None]:
    with worker_names.roles_lock():
        yield


def _write_role(role: str, label: str, *, why: str = "by hand",
                cause: Literal["by hand", "refusal", "auto", "credits off"] = "by hand") -> None:
    _validate_role(role)
    if role != "workers":
        raise AccountsError(UNIT_ROLE_GONE)
    write_role_map(RoleMapChange("workers", note_file.canonical_label(label), why,
                               datetime.now(timezone.utc).isoformat(), cause=cause))


def set_role(role: str, label: str) -> None:
    """Atomically select a readable account and preserve its note identity."""
    label = note_file.canonical_label(label)
    _validate_role(role)
    refuse_default(label)
    if role == "workers":
        if not role_map.is_role_map_host():
            _write_role(role, numbered_account_label(label, "codex").label)
            return
        target = check_codex_target(label, accounts_home() / ".codex", repair=False)
        _ = _write_role(role, target.label if _label_is(target.label, "codex") else label)
        return
    raise AccountsError(UNIT_ROLE_GONE)


def _shutdown_argv(*arguments: str) -> tuple[str, ...]:
    return (sys.executable, str(SCRIPTS / "shutdown" / "shutdown.py"), *arguments)


@dataclass(frozen=True)
class MoveRefused:
    reason: str


@dataclass(frozen=True)
class SessionMoveRequested:
    session_id: str
    request_id: str
    target: str
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    move_id: str = ""
    source_login: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if not self.move_id:
            object.__setattr__(self, "move_id", self.request_id)


@dataclass(frozen=True)
class SessionMoveRefused:
    session_id: str
    reason: str
    stdout: str = ""
    stderr: str = ""
    returncode: int = 1


def _move_down_argv(source: str, ids: tuple[str, ...], target: str, why: str, alerts: MoveAlerts) -> tuple[str, ...]:
    """Build the selected move's shutdown command, including its alert policy."""
    return _shutdown_argv("down", source, "--here", "--only", ",".join(ids),
        "--restart", "--to", target, "--why", why) + (("--no-alert",) if alerts == "none" else ())


@dataclass(frozen=True)
class MoveShutdownReady:
    lines: tuple[str, ...] = ()


def _prepare_move_shutdown(saved: SessionShutdown, *, dry_run: bool) -> MoveShutdownReady | MoveRefused:
    """Close only a proven live session's unconfirmed restart before moving it."""
    if TYPE_CHECKING:
        from scripts.shutdown import record
    else:
        record = import_module("record")
    restart = cast(RestartRecordLiveness, cast(object, import_module("restart")))
    settle = cast(ShutdownLocation, cast(object, import_module("settle")))
    if not restart.restart_failed_session_running(saved):
        return MoveRefused("already has a shutdown in progress")
    name = saved["session"]["name"]
    if dry_run:
        return MoveShutdownReady((f"{name}: would close its unconfirmed restart record; the session is running",))
    outcome = record.forget_failed_restart(saved["login"], saved["session"]["session_id"],
                                         settle.local_machine(), saved["request_id"])
    if not isinstance(outcome, record.SessionForgotten):
        return MoveRefused("already has a shutdown in progress")
    _ = record.acknowledge_snapshot(saved["login"], saved["session"]["session_id"])
    return MoveShutdownReady((f"{name}: closed its unconfirmed restart record; the session is running",))


def move_sessions(ids: tuple[str, ...], target: str, *, why: str, origin: MoveOrigin, move_id: str = "", alerts: MoveAlerts = "user") -> tuple[SessionMoveRequested | SessionMoveRefused, ...]:
    """Start one selected shutdown for each source login and report each owner's record."""
    target = numbered_account_label(target, "claude").label
    from uuid import uuid4
    transition_id = move_id or uuid4().hex
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    if TYPE_CHECKING:
        from scripts.shutdown import inventory, record
    else:
        inventory = import_module("inventory")
        record = import_module("record")
    outcomes: dict[str, SessionMoveRequested | SessionMoveRefused] = {}
    owners: dict[str, ListedSession] = {}
    groups: dict[str, list[str]] = {}

    def refuse(session_id: str, reason: str, *, stdout: str = "", stderr: str = "", returncode: int = 1) -> None:
        outcomes[session_id] = SessionMoveRefused(session_id, reason, stdout, stderr, returncode)
        name = owners[session_id].name if session_id in owners else session_id
        moves.append_move(moves.FailedMove(kind="failed", at=datetime.now(timezone.utc).isoformat(),
            tool="claude", subject=session_id + ": " + name, move_id=transition_id, reason=reason))

    try:
        destination = _move_target(target)
        if isinstance(destination, MoveRefused):
            for session_id in ids:
                refuse(session_id, destination.reason)
            return tuple(outcomes[session_id] for session_id in ids)
        live = {item.session_id: item for item in inventory.listing()}
        before = {saved["session"]["session_id"]: saved for saved in record.all_session_records()}
        for session_id in ids:
            found = live.get(session_id)
            if found is None:
                refuse(session_id, "session is not live on this machine")
                continue
            if found.kind == "worker":
                refuse(session_id, "workers move with their unit director")
                continue
            owners[session_id] = found
            if found.login.casefold() == destination.login.casefold():
                refuse(session_id, "already on " + destination.label)
                continue
            if session_id in before:
                prepared = _prepare_move_shutdown(before[session_id], dry_run=False)
                if isinstance(prepared, MoveRefused):
                    refuse(session_id, prepared.reason)
                    continue
                for line in prepared.lines:
                    print(line)
            groups.setdefault(found.login, []).append(session_id)
        for login, selected_ids in groups.items():
            source = account.label_for("Claude", login)
            command = (*_move_down_argv(source, tuple(selected_ids), target, why, alerts),
                "--move-id", transition_id, *move_origin_arguments(origin))
            try:
                prior_requests = {(entry.get("request_id", entry["move_id"]), entry["subject"]) for entry in moves.read_moves()
                                  if entry["kind"] == "requested"}
                result = subprocess.run(command, capture_output=True, text=True, check=False,
                    env={**os.environ, "REFUSAL_WATCH_INVENTORY": "read existing"})
                saved_by_id = {saved["session"]["session_id"]: saved for saved in record.all_session_records()}
                for session_id in selected_ids:
                    saved = saved_by_id.get(session_id)
                    if saved is None or saved.get("target") != target:
                        # A fast conductor may already have archived a confirmed restart.
                        journal_request = next((entry for entry in reversed(moves.read_moves())
                            if entry["kind"] == "requested" and entry["tool"] == "claude" and entry["to"] == target
                            and entry["subject"] == session_id + ": " + owners[session_id].name
                            and (entry.get("request_id", entry["move_id"]), entry["subject"]) not in prior_requests), None)
                        if journal_request is not None:
                            outcomes[session_id] = SessionMoveRequested(session_id, journal_request.get("request_id", journal_request["move_id"]), target,
                                result.stdout, result.stderr, result.returncode, journal_request["move_id"], source_login=login)
                            continue
                        refuse(session_id, result.stderr.strip() or result.stdout.strip() or "shutdown refused before a record was created",
                            stdout=result.stdout, stderr=result.stderr, returncode=result.returncode or 1)
                        continue
                    outcome = SessionMoveRequested(session_id, saved["request_id"], target,
                        result.stdout, result.stderr, result.returncode, saved.get("move_id", saved["request_id"]), source_login=login)
                    outcomes[session_id] = outcome
                    if not any(entry["kind"] == "requested" and entry.get("request_id", entry["move_id"]) == saved["request_id"]
                            and entry["subject"] == session_id + ": " + owners[session_id].name for entry in moves.read_moves()):
                        request: moves.RequestedMove = {"kind": "requested", "at": saved["requested_at"],
                            "tool": "claude", "subject": session_id + ": " + owners[session_id].name,
                            "move_id": saved.get("move_id", saved["request_id"]), "request_id": saved["request_id"], "from": saved.get("source_label", source),
                            "to": target, "why": why, "window": "unknown"}
                        fields = move_origin_fields(origin)
                        request["cause"], request["window"] = fields["cause"], fields["window"]
                        if "reset" in fields:
                            request["reset"] = fields["reset"]
                        moves.append_move(request)
            except (OSError, ValueError, record.InvalidRecord) as error:
                for session_id in selected_ids:
                    refuse(session_id, str(error))
    except (AccountsError, OSError, ValueError, inventory.InvalidInventory, record.InvalidRecord) as error:
        for session_id in ids:
            if session_id not in outcomes:
                refuse(session_id, str(error))
    return tuple(outcomes[session_id] for session_id in ids)


@dataclass(frozen=True)
class MoveStarted:
    session_id: str
    target: str
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class MoveStartFailed:
    session_id: str
    target: str
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class GroupMoveStartFailed:
    session_ids: tuple[str, ...]
    target: str
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class GroupMoveDryRun:
    session_ids: tuple[str, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class GroupMoveStarted:
    session_ids: tuple[str, ...]
    target: str
    stdout: str
    stderr: str
    returncode: int


def _move_target(target: str, *, repair: bool = True) -> agent_accounts.NumberedAccount | MoveRefused:
    target = note_file.canonical_label(target)
    try:
        refuse_default(target)
        prepared = (prepared_account(target, resume=True) if repair
                    else prepared_account_preview(target, resume=True))
        if isinstance(prepared, AccountRefused):
            return MoveRefused(prepared.reason.partition("\n")[0])
        destination = account_identity(claude_account_dir(target))
    except AccountsError as error:
        return MoveRefused(str(error).partition("\n")[0])
    if not isinstance(destination, agent_accounts.NumberedAccount):
        return MoveRefused(f"account identity for {target} is unreadable")
    return destination


@dataclass(frozen=True)
class MoveTargetReady:
    destination: agent_accounts.NumberedAccount
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class MoveDryRun:
    session_id: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class MoveTargetHoldClear:
    notices: tuple[str, ...] = ()


def check_move_target_hold(target: str, *, dry_run: bool = False) -> MoveTargetHoldClear | MoveRefused:
    """Check the target before changing a group or stopping a session."""
    if dry_run:
        return MoveTargetHoldClear((f"{target}: would check the shutdown hold",))
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    if TYPE_CHECKING:
        from scripts.shutdown import launch_permission
    else:
        launch_permission = import_module("launch_permission")
    environment = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_account_dir(target))}
    permission = launch_permission.launch_permission(environment, launch_permission.NewWorkLaunchPurpose())
    if isinstance(permission, launch_permission.BlockedByShutdown):
        return MoveRefused(f"accounts: {permission.reason}; no session was stopped")
    if isinstance(permission, launch_permission.ShutdownStateUnreadable):
        return MoveRefused(f"accounts: shutdown state unreadable: {permission.detail}; no session was stopped")
    released = permission.released
    if released is None:
        return MoveTargetHoldClear()
    requester = (f"session {released.session_id[:8]}" if released.session_id is not None
                 else "requested in a terminal or by a recovery snapshot")
    at = datetime.fromisoformat(released.requested_at).astimezone(agent_accounts.user_zone())
    return MoveTargetHoldClear((f"cleared a finished {released.then} hold on {released.label} "
                               + f"from {at:%Y-%m-%d %H:%M %Z} ({requester})",))


def check_move_target(target: str, now: datetime, *, repair: bool = True) -> MoveTargetReady | MoveRefused:
    """Check the destination without changing a group or starting a shutdown."""
    hold = check_move_target_hold(target, dry_run=not repair)
    if isinstance(hold, MoveRefused):
        return hold
    destination = _move_target(target, repair=repair)
    if isinstance(destination, MoveRefused):
        return destination
    reading = refusals.stored_reading("claude", destination.login, now)
    if isinstance(reading, FreshReading):
        try:
            credits = float(reading.credits or "0")
        except ValueError:
            credits = 0.0
        if reading.credits != "unlimited" and (not math.isfinite(credits) or credits <= 0):
            for name, window in (("weekly", reading.weekly), ("5-hour", reading.five_hour)):
                if isinstance(window, ReadingWindow) and window.left <= 0:
                    reset = (window.reset.at.astimezone(agent_accounts.user_zone()).strftime("%a %-I:%M %p")
                             if isinstance(window.reset, KnownReset) else "unknown")
                    chosen = pick("claude", now, exclude=frozenset({destination.label}), keep=NoKeep())
                    room = f"{chosen.label} has room" if isinstance(chosen, PickedAccount) else no_room_line("claude", chosen)
                    return MoveRefused(f"accounts: {destination.label} is out of usage ({name} {window.left:g}% left, resets {reset}); {room}")
        return MoveTargetReady(destination, hold.notices)
    windows = ((reading.weekly, reading.five_hour) if isinstance(reading, StaleReading) else ())
    times = [window.at for window in windows if isinstance(window, ReadingWindow)]
    age = f"last {max(0, int((now - max(times)).total_seconds() // 60))} minutes ago" if times else "last reading unknown"
    return MoveTargetReady(destination, (*hold.notices, f"no fresh usage reading for {destination.label} ({age})"))


def move_preview_line(member: ListedSession, now: datetime) -> str:
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    reader = cast(MovePromptReader, cast(object, import_module("settle")))
    state = reader.prompt_state(reader.PromptSubject(member.session_id, member.login, member.config_dir), now)
    source = session_account_label(member)
    if state.because in {"refused in its transcript", "account out of usage"}:
        plan = f"will stop and move now ({source} out of usage: {cast(MemberNeedingReap, state).detail})"
    else:
        reason = "has room" if state.because == "room" else state.because
        plan = f"will move once idle ({source} {reason})"
    return f"{member.name}: {member.session_id} · {plan}"


def _move_preview_commands(members: list[ListedSession], target: str, why: str, alerts: MoveAlerts) -> tuple[str, ...]:
    """Show the planned shutdown commands when a preview suppresses alerts."""
    if alerts == "user":
        return ()
    groups: dict[str, list[str]] = {}
    for member in members:
        groups.setdefault(member.login, []).append(member.session_id)
    return tuple(shlex.join(_move_down_argv(account.label_for("Claude", login), tuple(ids), target, why, alerts))
                 for login, ids in groups.items())


def follow_move(login: str, request_id: str) -> int:
    """Follow a local move request using shutdown's archive-aware trail reader."""
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    follower = cast(MoveFollower, cast(object, import_module("shutdown")))
    return follower.follow(login, request_id)


def _move_output(results: tuple[SessionMoveRequested | SessionMoveRefused, ...],
                 notices: tuple[str, ...], back_subject: str,
                 back_target: str | GroupNotConfigured) -> tuple[str, str, int]:
    stdout = ("\n".join(notices) + "\n" if notices else "") + "".join(dict.fromkeys(item.stdout for item in results))
    stderr = "".join(dict.fromkeys(item.stderr for item in results))
    refused = [item for item in results if isinstance(item, SessionMoveRefused)]
    reasons = tuple(f"{item.session_id}: {item.reason}" for item in refused if not item.stderr)
    if reasons:
        stderr += "\n".join(reasons) + "\n"
    code = max((item.returncode for item in results), default=0) or (1 if refused else 0)
    requests = dict.fromkeys((item.source_login, item.request_id)
                            for item in results if isinstance(item, SessionMoveRequested))
    if requests and stdout and not stdout.endswith("\n"):
        stdout += "\n"
    sources = {request[0] for request in requests}
    if isinstance(back_target, GroupNotConfigured) and len(sources) == 1:
        back_target = account.label_for("Claude", next(iter(sources)))
    if requests and isinstance(back_target, str):
        stdout += ("to move them back, in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move "
                   + shlex.quote(back_subject) + " " + shlex.quote(back_target) + "\n")
    if requests and not os.environ.get("CLAUDE_CODE_SESSION_ID") and sys.stdout.isatty():
        print(stdout, end="", flush=True)
        stdout = ""
        for login, request_id in requests:
            followed = follow_move(login, request_id)
            if followed == 130:
                code = 130
                break
            code = max(code, followed)
    return stdout, stderr, code


@role_map.role_map_command
def move_group(group: ClaudeGroup, target: str, *, dry_run: bool = False, why: str = "", alerts: MoveAlerts = "user") -> GroupMoveStarted | GroupMoveStartFailed | GroupMoveDryRun | MoveRefused:
    """Select the group after target checks, then hand each owner to shutdown."""
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    target = note_file.canonical_label(target)
    now = datetime.now(timezone.utc)
    checked = check_move_target(target, now, repair=not dry_run)
    if isinstance(checked, MoveRefused):
        return checked
    if TYPE_CHECKING:
        from scripts.shutdown import inventory, record
    else:
        inventory = import_module("inventory")
        record = import_module("record")
    registrations = inventory.showrunner_registrations()
    members = live_group_members(registrations=registrations)[group]
    selected = [session for session in members if not session_on_account(session, target)]
    ids = tuple(session.session_id for session in selected)
    reason = why or f"{group} moved by hand"
    notices = (f"{group}: no live session moves",) if not ids else ()
    if group == "showrunners":
        notices += tuple(
            f"{runner['slug']}: no session runs under the id its update timer names; "
            + f"in any Claude session on {socket.gethostname().split('.', 1)[0]}, inside that showrunner's own session: ~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py register --production {shlex.quote(runner['doc'])}"
            for runner in inventory.unregistered_showrunners(members, registrations=registrations))
    if dry_run:
        closing: tuple[str, ...] = ()
        for saved in record.all_session_records():
            if saved["session"]["session_id"] not in ids:
                continue
            prepared = _prepare_move_shutdown(saved, dry_run=True)
            if isinstance(prepared, MoveShutdownReady):
                closing += prepared.lines
        return GroupMoveDryRun(ids, (*checked.notices, f"{group}: would select {target}",
            *closing,
            *(move_preview_line(item, now) for item in selected),
            *_move_preview_commands(selected, target, reason, alerts), *notices))
    previous_target = group_label(group_accounts(), group)
    transition = set_group_account(group, target, why=reason)
    results = move_sessions(ids, target, why=reason, origin=transition.origin, move_id=transition.move_id, alerts=alerts) if ids else ()
    stdout, stderr, code = _move_output(results, (*checked.notices, *notices), group, previous_target)
    outcome = GroupMoveStartFailed if code else GroupMoveStarted
    return outcome(ids, target, stdout, stderr, code)


def session_on_account(session: ListedSession, label: str) -> bool:
    if not _label_is(label, "claude"):
        return False
    destination = account_identity(claude_account_dir(label))
    return (isinstance(destination, agent_accounts.NumberedAccount)
            and session.login.casefold() == destination.login.casefold())


@overload
def move(session: str, target: str, *, dry_run: Literal[False] = False, why: str = "", alerts: MoveAlerts = "user") -> MoveStarted | MoveStartFailed | MoveRefused: ...


@overload
def move(session: str, target: str, *, dry_run: Literal[True], why: str = "", alerts: MoveAlerts = "user") -> MoveDryRun | MoveRefused: ...


@overload
def move(session: str, target: str, *, dry_run: bool, why: str = "", alerts: MoveAlerts = "user") -> MoveStarted | MoveStartFailed | MoveDryRun | MoveRefused: ...


def move(session: str, target: str, *, dry_run: bool = False, why: str = "", alerts: MoveAlerts = "user") -> MoveStarted | MoveStartFailed | MoveDryRun | MoveRefused:
    """Move one local ungrouped owner after read-only destination checks."""
    target = note_file.canonical_label(target)
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    if TYPE_CHECKING:
        from scripts.shutdown import inventory, record
    else:
        inventory = import_module("inventory")
        record = import_module("record")
    try:
        now = datetime.now(timezone.utc)
        checked = check_move_target(target, now, repair=not dry_run)
        if isinstance(checked, MoveRefused):
            return checked
        live = inventory.listing()
        matches = [item for item in live if item.session_id == session]
        if not matches:
            matches = [item for item in live if item.name == session]
        if not matches:
            return MoveRefused(f"no live session by id or name {session} on this machine; a session on the other machine moves from there")
        if len(matches) != 1:
            return MoveRefused(f"{session} names multiple live sessions: " + ", ".join(item.session_id for item in matches))
        owner = matches[0]
        if owner.kind == "worker":
            return MoveRefused("workers move with their unit director")
        group = session_group(owner)
        if group != "ungrouped":
            return MoveRefused(f"{owner.name} belongs to {group}; in this terminal: ~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move {group} {target}")
        if session_on_account(owner, target):
            return MoveRefused(f"{owner.name} is already on {checked.destination.label}")
        closing: tuple[str, ...] = ()
        for saved in record.all_session_records():
            if saved["session"]["session_id"] != owner.session_id:
                continue
            prepared = _prepare_move_shutdown(saved, dry_run=True)
            if isinstance(prepared, MoveRefused):
                return MoveRefused(f"{owner.name} {prepared.reason}")
            closing += prepared.lines
        reason = why or f"{session_account_label(owner)} is out of usage"
        if dry_run:
            return MoveDryRun(owner.session_id, (*checked.notices, *closing, move_preview_line(owner, now),
                *_move_preview_commands([owner], target, reason, alerts)))
        results = move_sessions((owner.session_id,), target, why=reason, origin=HAND_ORIGIN, alerts=alerts)
        stdout, stderr, code = _move_output(results, checked.notices, owner.session_id, GROUP_NOT_CONFIGURED)
        outcome = MoveStartFailed if code else MoveStarted
        return outcome(owner.session_id, target, stdout, stderr, code)
    except (AccountsError, record.InvalidRecord, inventory.InvalidInventory, OSError, ValueError) as error:
        return MoveRefused(str(error))


def check_codex_target(label: str, workers_home: Path, *, repair: bool = True) -> SelectableCodexAccount:
    label = note_file.canonical_label(label)
    refuse_default(label)
    home = _codex_account_home(label)
    problems = (_repair_shared_links if repair else _shared_repair_problems)(
        home, (accounts_home() / ".codex").absolute(), CODEX_SHARED, CODEX_ALWAYS_LINKED,
    )
    if problems:
        raise AccountsError(problems[0])
    if home.is_symlink() or (home / "auth.json").is_symlink():
        problems = _check_codex_home(label, workers_home)
        raise AccountsError(problems[0])
    identity = agent_accounts.codex_account(home)
    if not isinstance(identity, agent_accounts.KnownCodexAccount):
        login = _codex_home_login(home)
        legacy_home = accounts_home() / ".codex"
        noted_legacy = isinstance(agent_accounts.codex_account(legacy_home), agent_accounts.KnownCodexAccount)
        home_login = "" if noted_legacy else _codex_home_login(legacy_home)
        if login or home_login and not home.exists():
            raise AccountsError("accounts: " + unnoted_home_line(home if login else accounts_home() / ".codex", login or home_login))
    problems = _check_codex_home(label, workers_home, allow_shared_repair=not repair)
    if problems:
        problem = problems[0].removeprefix("accounts: ")
        raise AccountsError(f"accounts: cannot select {label} ({_codex_folder(home)}): {problem}")
    login = _codex_home_login(home)
    if not login:
        raise AccountsError(f"accounts: {label} ({_codex_folder(home)}) has no readable sign-in; check it and try again")
    if not isinstance(identity, agent_accounts.KnownCodexAccount):
        raise AccountsError("accounts: " + unnoted_home_line(home, login))
    return SelectableCodexAccount(home, login, identity.label)


def codex_home_label(home: Path) -> str:
    """The account's note label, or its folder when the login has no note."""
    return agent_accounts.codex_home_role_label(home)


@dataclass(frozen=True)
class FoldedWorkerRow:
    index: int
    context: str
    mid_turn_count: int
    count: int


def _retire_workers_servers(home: Path, label: str, *, dry_run: bool) -> Sequence[WorkersRunSwitch]:
    from scripts.agents.codex_mesh import retire_workers_servers

    return retire_workers_servers(home, label, dry_run=dry_run)


def _workers_swap_lines(switches: Sequence[WorkersRunSwitch], previous: Path,
                        target: str, *, dry_run: bool) -> tuple[str, ...]:
    from scripts.agents.codex_mesh import leftover_server_lines
    from scripts.message import roster, sessions

    old = agent_accounts.codex_account(previous)
    source = old.label + " → " if isinstance(old, agent_accounts.KnownCodexAccount) else ""
    lines = [f"Workers {'would move' if dry_run else 'moving'} {source}{target}:"]
    active = [switch for switch in switches if switch.workers]
    if not active:
        return (*lines, *leftover_server_lines(switches, dry_run=dry_run))
    listing = roster.roster(os.environ, roster.ThisMachine())
    holders = {entry.session.session_id: entry for entry in listing.entries
               if isinstance(entry.session, roster.ClaudeSession)}
    saved_names: dict[str, str] = {}
    if any(not any(session_id in holders for session_id in (switch.holders or switch.launcher_sessions))
           for switch in active):
        try:
            paths = sessions.sessions_dir().glob("*.json")
            records = [sessions.read_session(path) for path in paths]
        except OSError:
            records = []
        readable = [record for record in records if not isinstance(record, sessions.UnreadableSessionRecord)]
        for record in sorted(readable, key=lambda item: item["updatedAt"], reverse=True):
            if record["name"]:
                _ = saved_names.setdefault(record["sessionId"], record["name"])
    folded: dict[tuple[str, str], FoldedWorkerRow] = {}
    for switch in active:
        naming_sessions = switch.holders or switch.launcher_sessions
        holder_id = next((session_id for session_id in naming_sessions if session_id in holders),
                         next(iter(naming_sessions), str(switch.folder)))
        holder = holders.get(holder_id)
        parts: list[str] = []
        if holder is not None:
            if holder.machine != roster.machine_name():
                parts.append(holder.machine)
            if isinstance(holder.showrunner, roster.UnderShowrunner):
                parts.append(holder.showrunner.name)
            elif isinstance(holder.showrunner, (roster.RunsProduction, roster.ProductionWithoutRunningShowrunner)):
                parts.append(holder.showrunner.production)
            parts.append(holder.director.unit if isinstance(holder.director, (roster.DirectsUnit, roster.UnderDirector))
                         and holder.director.unit else holder.name)
        else:
            parts.append(next((saved_names[session_id] for session_id in naming_sessions if session_id in saved_names),
                              f"unknown session ({switch.folder.name})"))
        for worker in switch.workers:
            stem = worker.name.removesuffix("-worker")
            slot = stem.rpartition("-")[2] or stem
            name = f"{slot}-worker"
            context = " · ".join([*parts, name])
            if worker.failure:
                lines.append(f"  {context} → {target}, {worker.failure}; resume with {worker.resume_command}")
                continue
            key = (holder_id, slot)
            previous_row = folded.get(key, FoldedWorkerRow(len(lines), context, 0, 0))
            row = replace(previous_row, mid_turn_count=previous_row.mid_turn_count + int(worker.mid_turn),
                          count=previous_row.count + 1)
            copies = f" ×{row.count}" if row.count > 1 else ""
            if row.mid_turn_count == 0:
                suffix = ""
            elif row.count == 1:
                suffix = ", interrupted and resumed"
            else:
                suffix = f", {row.mid_turn_count} interrupted and resumed"
            line = f"  {row.context} → {target}{copies}{suffix}"
            if row.index == len(lines):
                lines.append(line)
            else:
                lines[row.index] = line
            folded[key] = row
    return (*lines, *leftover_server_lines(switches, dry_run=dry_run))


def _swap_workers(label: str, *, dry_run: bool, why: str = "by hand",
                  cause: Literal["by hand", "refusal", "auto", "credits off"] = "by hand",
                  included_only: bool = False) -> WorkersSwapOutcome:
    """Check the live allowance before changing the workers account."""
    refuse_default(label)
    try:
        workers_home = _workers_home()
    except AccountsError:
        # An explicit swap can configure an unset/unavailable local workers role;
        # reading the authoritative map must still succeed before it proceeds.
        _ = read_role_map()
        workers_home = accounts_home() / ".codex"
    target = check_codex_target(label, workers_home)
    account_label, target_home = target.label, target.home
    if target_home == workers_home and read_role_map().workers:
        raise AccountsError(f"accounts: {account_label} ({_codex_folder(target_home)}) already serves workers; choose another account")
    report = _codex_live(target_home)
    if report.problem:
        raise AccountsError(f"accounts: cannot use {account_label} ({_codex_folder(target_home)}): {report.problem}")
    if (not report.email or report.email.casefold() != target.login.casefold()
            or _codex_home_login(target_home).casefold() != target.login.casefold()):
        raise AccountsError(f"accounts: cannot read {account_label} ({_codex_folder(target_home)}); check its sign-in and try again")
    credits = agent_accounts.CreditsOff() if included_only else agent_accounts.codex_credits_setting(account_label)
    readiness = agent_accounts.codex_can_work(report, datetime.now(timezone.utc), credits)
    if isinstance(readiness, agent_accounts.CodexWorkExhausted):
        if included_only:
            raise AccountsError(f"accounts: {account_label} ({_codex_folder(target_home)}) has no included usage left; choose another account or wait for reset")
        reason = "and set not to use credits" if isinstance(credits, agent_accounts.CreditsOff) else "with no credits"
        raise AccountsError(f"accounts: {account_label} ({_codex_folder(target_home)}) is used up {reason}; choose another account or wait for reset")
    if isinstance(readiness, agent_accounts.CodexWorkUnknown):
        raise AccountsError(f"accounts: cannot use {account_label} ({_codex_folder(target_home)}): {readiness.problem}")
    if dry_run:
        switches = _retire_workers_servers(target_home, account_label, dry_run=True)
        return WorkersSwapDryRun(_workers_swap_lines(switches, workers_home, account_label, dry_run=True))
    try:
        _ = _write_role("workers", account_label if _label_is(account_label, "codex") else label, why=why, cause=cause)
    except OSError as error:
        raise AccountsError(f"accounts: could not move the workers role: {error}") from error
    try:
        switches = _retire_workers_servers(target_home, account_label, dry_run=False)
        lines = _workers_swap_lines(switches, workers_home, account_label, dry_run=False)
    except (ConnectionError, OSError, SystemExit) as error:
        lines = (f"Workers role now uses {account_label}; worker move failed: {error}. "
                 + "Resume affected workers with codex_mesh.py resume-account-move --session-dir <run> --to <worker>.",)
    return WorkersSwapStarted(lines)


def swap(role: str, label: str, *, dry_run: bool = False, why: str = "by hand",
         cause: Literal["by hand", "refusal", "auto", "credits off"] = "by hand",
         included_only: bool = False) -> SwapOutcome | WorkersSwapOutcome:
    """Move workers; included_only requires room without paid credits at the live check."""
    _validate_role(role)
    if role == "workers":
        return _swap_workers(label, dry_run=dry_run, why=why, cause=cause, included_only=included_only)
    raise AccountsError(UNIT_ROLE_GONE)


def _is_known_private(name: str) -> bool:
    return (
        name in KNOWN_PRIVATE_NAMES
        or name.startswith("daemon-auth-")
        or name.startswith(".oauth_refresh.lock")
        or name.startswith(".claude.json.tmp.")
    )


def sweep_claude_temp_files(now: datetime) -> None:
    """Remove only regular atomic-write remnants older than a day, without following links."""
    home = accounts_home()
    folders = [home]
    stale_before = (now - timedelta(days=1)).timestamp()
    for folder in folders:
        try:
            descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    try:
                        if (folder == home and re.fullmatch(r"\.claude[1-9][0-9]*", entry.name)
                                and entry.is_dir(follow_symlinks=False)):
                            folders.append(folder / entry.name)
                        if not entry.name.startswith(".claude.json.tmp."):
                            continue
                        metadata = os.lstat(entry.name, dir_fd=descriptor)
                        if stat.S_ISREG(metadata.st_mode) and metadata.st_mtime < stale_before:
                            os.unlink(entry.name, dir_fd=descriptor)
                    except OSError:
                        continue
        except OSError:
            continue
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _unclassified(config_dir: Path) -> tuple[str, ...]:
    try:
        names = (
            entry.name
            for entry in config_dir.iterdir()
            if entry.name not in SHARED
            and not _is_known_private(entry.name)
        )
        return tuple(sorted(names))
    except OSError:
        return ()


def _mcp_server_names(config_file: Path) -> frozenset[str]:
    try:
        servers = _read_json_object(config_file).get("mcpServers")
    except (OSError, ValueError, TypeError):
        return frozenset()
    if not isinstance(servers, dict):
        return frozenset()
    return frozenset(
        name
        for name in cast(dict[object, object], cast(object, servers))
        if isinstance(name, str)
    )


def _existing_named_accounts() -> list[NamedAccount]:
    try:
        children = accounts_home().iterdir()
    except OSError:
        return []
    accounts: list[NamedAccount] = []
    for child in children:
        match = re.fullmatch(r"\.claude([1-9][0-9]*)", child.name)
        if match is not None:
            accounts.append(NamedAccount(note_file.NumberedAccountLabel("claude", int(match.group(1))).label, child))
    return sorted(accounts, key=lambda account: _label_number(account.label))


def _status_for(config_dir: Path, folder: str, named: NamedAccount, problems: list[str]) -> AccountStatus:
    baseline = _mcp_server_names(accounts_home() / ".claude.json")
    account_is_safe_to_inspect = not config_dir.is_symlink() and not (
        (config_dir / ".claude.json").is_symlink()
    )
    # A named folder keeps its own label whatever login it holds; `problems` names a mismatch.
    label = named.label
    login = ""
    if account_is_safe_to_inspect:
        identity = account_identity(config_dir)
        if isinstance(identity, (agent_accounts.NumberedAccount, agent_accounts.UnnotedLogin)):
            login = identity.login
            if isinstance(identity, agent_accounts.UnnotedLogin):
                problems.append(unnoted_home_line(config_dir, identity.login))
        config_file = config_dir / ".claude.json"
        current = _mcp_server_names(config_file)
        unclassified = _unclassified(config_dir)
    else:
        current = baseline
        unclassified = ()
    return AccountStatus(
        folder=folder,
        config_dir=config_dir,
        is_named_account=True,
        is_set_up=True,
        account_is_safe_to_inspect=account_is_safe_to_inspect,
        label=label,
        login=login,
        role="",
        problems=tuple(problems),
        unclassified=unclassified,
        missing_mcp_servers=tuple(sorted(baseline - current)),
        extra_mcp_servers=tuple(sorted(current - baseline)),
    )


def _label_number(label: str) -> int:
    parsed = note_file.parse_account_label(label)
    if not isinstance(parsed, note_file.NumberedAccountLabel):
        raise AccountsError(f"accounts: {label} is not a numbered account label")
    return parsed.number


def _noted_labels() -> set[str]:
    """Every `claudeN` label that has an account note, with or without a folder."""
    return {
        note.label for note in note_file.read_notes() if note.tool == "claude"
    }


def account_statuses() -> list[AccountStatus]:
    """Inspect numbered folders and show noted accounts not yet set up."""
    statuses: list[AccountStatus] = []
    on_disk = {named.label: named for named in _existing_named_accounts()}
    for label in sorted({*on_disk, *_noted_labels()}, key=_label_number):
        folder = f"~/.claude{_label_number(label)}"
        named = on_disk.get(label)
        if named is not None:
            statuses.append(_status_for(named.dir, folder, named, check(label)))
            continue
        statuses.append(AccountStatus(
            folder=folder,
            config_dir=account_dir(label),
            is_named_account=True,
            is_set_up=False,
            account_is_safe_to_inspect=True,
            label=label,
            login="",
            role=NO_VALUE,
            problems=(),
            unclassified=(),
            missing_mcp_servers=(),
            extra_mcp_servers=(),
        ))
    return statuses


def codex_statuses(workers_home: Path) -> list[CodexStatus]:
    """Every listed home, then unheld noted accounts whose home is not listed."""
    statuses: list[CodexStatus] = []
    homes = agent_accounts.codex_homes(workers_home)
    for home in homes:
        folder = _codex_folder(home)
        safe = not home.is_symlink() and not (home / "auth.json").is_symlink()
        login = _codex_home_login(home)
        if home == accounts_home() / ".codex" and not login:
            continue
        label = codex_home_label(home)
        match = re.fullmatch(r"\.codex([1-9][0-9]*)", home.name) if home.parent == accounts_home() else None
        problems = _check_codex_home(label, workers_home) if _label_is(label, "codex") else _check_default_codex_home(workers_home) if home == accounts_home() / ".codex" else []
        if login and not _label_is(label, "codex"):
            problems.append(unnoted_home_line(home, login))
        if match and not login:
            intended = note_file.NumberedAccountLabel("codex", int(match.group(1))).label
            expected = _account_note(intended)
            hint = intended if isinstance(expected, ExpectedAccountLogin) else label
            problems.append(f"accounts: sign in as {hint} at {folder}")
        unclassified: tuple[str, ...] = ()
        if match and safe:
            try:
                unclassified = tuple(sorted(entry.name for entry in home.iterdir()
                    if entry.name not in CODEX_SHARED and entry.name not in CODEX_PRIVATE_NAMES))
            except OSError as error:
                problem = f"accounts: could not inspect {home}: {error}"
                if problem not in problems:
                    problems.append(problem)
        statuses.append(CodexStatus(
            folder, home, True, safe, NO_VALUE if label == login else label, login,
            CODEX_ROLE if home == workers_home else "spare", tuple(problems), unclassified,
        ))
    held_logins = {status.login.casefold() for status in statuses if status.login}
    noted = sorted(
        (note.label for note in note_file.read_notes() if note.tool == "codex"),
        key=lambda stem: _label_number(stem),
    )
    for label in noted:
        home = _codex_account_home(label)
        expected = _account_note(label)
        if home in homes or (
            isinstance(expected, ExpectedAccountLogin) and expected.login.casefold() in held_logins
        ):
            continue
        problems = tuple(_check_codex_home(label, workers_home)) if os.path.lexists(home) else ()
        statuses.append(CodexStatus(
            f"~/{home.name}", home, False, not home.is_symlink(), label, "", NO_VALUE, problems, (),
        ))
    return statuses


def _codex_live(home: Path) -> agent_accounts.Report:
    return asyncio.run(agent_accounts.codex_live(home))


def _stored_time(text: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _read_at(usage: usage_store.StoredUsage | None) -> datetime | None:
    """When the oldest of a login's stored windows was read. None when it has no weekly window."""
    if usage is None or "weekly" not in usage:
        return None
    windows = [usage["weekly"], *([usage["five_hour"]] if "five_hour" in usage else [])]
    times = [_stored_time(window["at"]) for window in windows]
    return None if None in times else min(time for time in times if time is not None)


def _is_stale(usage: usage_store.StoredUsage | None, now: datetime) -> bool:
    read = _read_at(usage)
    return read is None or now - read > STALE_AFTER


@dataclass(frozen=True)
class RefreshStaleUsage:
    """Refresh signed-in accounts whose last good usage reading is stale."""


@dataclass(frozen=True)
class StoredUsageOnly:
    """Observe saved usage and roles without starting processes or networking."""


UsageSource = RefreshStaleUsage | StoredUsageOnly
REFRESH_STALE_USAGE = RefreshStaleUsage()


def _stored_usage(
    statuses: list[AccountStatus], codex: list[CodexStatus], now: datetime,
    source: UsageSource = REFRESH_STALE_USAGE,
) -> dict[str, usage_store.StoredUsage]:
    """Each login's stored readings. A login with none read lately is read live first, all at once."""
    stored = usage_store.load()
    if isinstance(source, StoredUsageOnly):
        return stored
    stale = [
        status.config_dir for status in statuses
        if status.login and _is_stale(stored.get(usage_store.key("Claude", status.login)), now)
    ]
    stale_codex = [status.home for status in codex
                   if status.login and _is_stale(stored.get(usage_store.key("Codex", status.login)), now)]
    if stale or stale_codex:
        with ThreadPoolExecutor() as pool:
            reads: list[tuple[Future[agent_accounts.Report],
                              agent_accounts.ClaudeDirectorySource | agent_accounts.CodexHomeSource, str]] = [
                     (pool.submit(agent_accounts.claude_live, status.config_dir),
                      agent_accounts.ClaudeDirectorySource(status.config_dir), status.login)
                     for status in statuses if status.config_dir in stale]
            reads.extend((pool.submit(_codex_live, status.home),
                          agent_accounts.CodexHomeSource(status.home), status.login)
                         for status in codex if status.home in stale_codex)
            reports = [replace(read.result(), source=directory,
                               last_known_login=agent_accounts.KnownUsageLogin(login))
                       for read, directory, login in reads]
        for report in reports:
            startup = report.codex_startup
            if isinstance(startup, agent_accounts.CodexStartupFailure):
                for index, status in enumerate(codex):
                    if status.home == startup.home:
                        codex[index] = replace(status, startup=startup, problems=(*status.problems, startup.problem))
        try:
            usage_store.save(reports, now)
            stored = usage_store.load()
        except OSError:
            # Keep this reading visible even if the store cannot be written.
            _ = usage_store.merge(stored, reports, now)
    return stored


def _is_current(window: usage_store.StoredWindow, now: datetime) -> bool:
    """Whether a stored window still describes now. One that has ended says nothing about the one after it."""
    if not window["resets"]:
        return True
    resets = _stored_time(window["resets"])
    return resets is not None and resets > now


@dataclass(frozen=True)
class StatusUnavailable:
    """No usable value; cell retains the table's reason or dash."""
    cell: str = NO_VALUE


@dataclass(frozen=True)
class WeeklyNotReadSince:
    """The last reading predates a weekly reset; it cannot describe this week's allowance."""

    read_at: datetime
    reason: agent_accounts.StaleTokenSkip | agent_accounts.NoWeeklyReadSkip


@dataclass(frozen=True)
class StatusRow:
    """One account's values, shared by the table and JSON renderers."""
    label: str
    tool: Literal["claude", "codex"]
    folder: str
    login: str
    roles: tuple[str, ...]
    weekly_left_percent: float | StatusUnavailable | WeeklyNotReadSince
    resets_at: datetime | StatusUnavailable
    credits: float | Literal["unlimited"] | StatusUnavailable
    on_credits: str
    use_credits: bool
    read_at: datetime | StatusUnavailable
    read_outcome: agent_accounts.UsageReadOutcome
    attempted_at: datetime | StatusUnavailable


@dataclass(frozen=True)
class StatusReading:
    rows: tuple[StatusRow, ...]
    notes: tuple[str, ...]
    detail_lines: tuple[str, ...]
    zone: tzinfo
    checked_at: datetime


class StatusAccountDocument(TypedDict):
    label: str
    tool: Literal["claude", "codex"]
    roles: list[str]
    weekly_left_percent: float | None
    resets_at: str | None
    credits: float | Literal["unlimited"] | None
    use_credits: bool
    read_at: str | None
    read_ok: bool
    credits_in_use: bool


class StatusDocument(TypedDict):
    accounts: list[StatusAccountDocument]
    problems: list[str]


def _credit_reading(
    usage: usage_store.StoredUsage, now: datetime,
) -> tuple[str, float | Literal["unlimited"] | StatusUnavailable]:
    credits = usage.get("credits")
    if credits is None:
        return ("unavailable", StatusUnavailable("unavailable"))
    balance = credits["balance"]
    try:
        amount = math.inf if balance == "unlimited" else float(balance)
    except ValueError:
        return ("unavailable", StatusUnavailable("unavailable"))
    if math.isnan(amount):
        return ("unavailable", StatusUnavailable("unavailable"))
    left: float | Literal["unlimited"] = "unlimited" if math.isinf(amount) else amount
    windows = [window for window in (usage.get("five_hour"), usage.get("weekly")) if window is not None]
    current = [window for window in windows if _is_current(window, now)]
    if any(window["left"] <= 0 for window in current):
        return ("yes" if amount > 0 else "no", left)
    return ("no" if "weekly" in usage and len(current) == len(windows) else "unavailable", left)


def _status_row(
    status: AccountStatus | CodexStatus, usage: usage_store.StoredUsage | None, now: datetime,
    roles: tuple[str, ...],
) -> StatusRow:
    tool = "codex" if isinstance(status, CodexStatus) else "claude"
    weekly: float | StatusUnavailable | WeeklyNotReadSince = StatusUnavailable()
    resets: datetime | StatusUnavailable = StatusUnavailable()
    credits: float | Literal["unlimited"] | StatusUnavailable = StatusUnavailable()
    read_at: datetime | StatusUnavailable = StatusUnavailable()
    on_credits = NO_VALUE
    login = status.login if status.is_set_up and status.account_is_safe_to_inspect else ""
    if login:
        read_at = _read_at(usage) or StatusUnavailable()
        if isinstance(status, CodexStatus) and isinstance(status.startup, agent_accounts.CodexStartupFailure):
            weekly = StatusUnavailable(status.startup.setup_command)
        else:
            held = usage or usage_store.StoredUsage()
            window = held.get("weekly")
            if window is not None and _is_current(window, now):
                weekly = window["left"]
                resets = _stored_time(window["resets"]) or StatusUnavailable()
            else:
                weekly = StatusUnavailable("unavailable")
                resets = StatusUnavailable("unavailable")
            if (window is not None and isinstance(read_at, datetime)
                    and (reset := _stored_time(window["resets"])) is not None and reset <= now):
                weekly = WeeklyNotReadSince(read_at, usage_store.weekly_read_skip(held))
            if tool == "codex":
                on_credits, credits = _credit_reading(held, now)
    return StatusRow(status.label, tool, status.folder, login, roles,
        weekly, resets, credits, on_credits,
        tool == "codex" and isinstance(agent_accounts.codex_credits_setting(status.label), agent_accounts.CreditsOn),
        read_at, usage_store.latest_read_outcome(usage or usage_store.StoredUsage()) if login
        else agent_accounts.UsageReadOutcome.OK,
        _stored_time((usage or {}).get("attempted_at", "")) or StatusUnavailable())


def _status_row_cells(row: StatusRow, zone: tzinfo, checked_at: datetime) -> tuple[str, ...]:
    weekly = row.weekly_left_percent
    reset = row.resets_at
    credits = row.credits
    read = row.read_at
    role_cell = ", ".join(row.roles) if row.roles else NO_VALUE if row.tool == "codex" else ""
    if isinstance(weekly, WeeklyNotReadSince):
        weekly_cell = f"not read since {weekly.read_at.astimezone(zone):%H:%M}"
        if isinstance(weekly.reason, agent_accounts.StaleTokenSkip):
            weekly_cell += ": no session to refresh its login"
    else:
        weekly_cell = weekly.cell if isinstance(weekly, StatusUnavailable) else f"{weekly:.0f}%"
    failed = row.read_outcome != agent_accounts.UsageReadOutcome.OK
    if isinstance(read, StatusUnavailable):
        read_cell = read.cell
    else:
        local = read.astimezone(zone)
        date_format = "%m-%d %H:%M %Z" if failed and local.date() != checked_at.astimezone(zone).date() else "%H:%M %Z"
        read_cell = local.strftime(date_format)
    if failed:
        read_cell += "*"
    return (row.folder, row.label, role_cell,
        weekly_cell,
        reset.cell if isinstance(reset, StatusUnavailable) else reset.astimezone(zone).strftime("%a %m-%d %H:%M %Z"),
        row.on_credits,
        credits.cell if isinstance(credits, StatusUnavailable) else credits if isinstance(credits, str) else f"{credits:,.0f}",
        read_cell)


def render_status_json(reading: StatusReading) -> str:
    accounts: list[StatusAccountDocument] = []
    for row in reading.rows:
        item = StatusAccountDocument(label=row.label, tool=row.tool, roles=list(row.roles),
            weekly_left_percent=None if isinstance(row.weekly_left_percent, (StatusUnavailable, WeeklyNotReadSince)) else row.weekly_left_percent,
            resets_at=None if isinstance(row.resets_at, StatusUnavailable) else row.resets_at.astimezone(timezone.utc).isoformat(),
            credits=None if isinstance(row.credits, StatusUnavailable) else row.credits,
            use_credits=row.use_credits,
            read_at=None if isinstance(row.read_at, StatusUnavailable) else row.read_at.astimezone(timezone.utc).isoformat(),
            read_ok=isinstance(row.read_at, datetime) and row.read_outcome in (
                agent_accounts.UsageReadOutcome.OK, agent_accounts.UsageReadOutcome.HELD),
            credits_in_use=row.on_credits == "yes")
        accounts.append(item)
    return json.dumps(StatusDocument(accounts=accounts, problems=[note.removeprefix("* ") for note in reading.notes]))


def _read_failure_note(row: StatusRow, zone: tzinfo) -> str:
    previous = (f", so this shows the reading from {row.read_at.astimezone(zone):%H:%M}"
                if isinstance(row.read_at, datetime) else " and no reading is stored yet")
    match row.read_outcome:
        case agent_accounts.UsageReadOutcome.STALE_TOKEN:
            attempt = login_renewal_attempts().get(row.label)
            reason = (f"login token expired; a probe session at {attempt.at.astimezone(zone):%H:%M} failed; log in on it by hand"
                      if attempt is not None and isinstance(attempt.result, LoginRenewalFailed)
                      else "login token expired; the next notes pass renews it")
        case agent_accounts.UsageReadOutcome.REFUSED_401:
            reason = "the usage service did not accept this login; start a session on it to check it"
        case agent_accounts.UsageReadOutcome.RATE_LIMITED_429:
            retry = (f"after {(row.attempted_at + timedelta(minutes=10)).astimezone(zone):%H:%M}"
                     if isinstance(row.attempted_at, datetime) else "in 2 min")
            reason = f"the usage service is busy{previous}; next try {retry}"
        case agent_accounts.UsageReadOutcome.ERROR:
            reason = f"the usage read failed{previous}; next try in 2 min"
        case agent_accounts.UsageReadOutcome.OK | agent_accounts.UsageReadOutcome.HELD:
            return ""
    return f"* {row.label}: {reason}"


def render_status_table(reading: StatusReading) -> str:
    return "\n".join([_render_table([_status_row_cells(row, reading.zone, reading.checked_at) for row in reading.rows]), "",
        *reading.notes, *reading.detail_lines])


def _render_table(rows: list[tuple[str, ...]]) -> str:
    """A Markdown table, padded so it also reads as plain text."""
    widths = [max(len(line[column]) for line in (TABLE_HEADINGS, *rows)) for column in range(len(TABLE_HEADINGS))]

    def rendered(cells: tuple[str, ...]) -> str:
        return "| " + " | ".join(cell.ljust(width) for cell, width in zip(cells, widths)) + " |"

    return "\n".join([
        rendered(TABLE_HEADINGS),
        rendered(tuple("-" * width for width in widths)),
        *(rendered(row) for row in rows),
    ])


def _account_state_notes(status: AccountStatus | CodexStatus) -> list[str]:
    """Explain an empty row unless a folder or sign-in check already explains it."""
    if status.is_set_up and status.account_is_safe_to_inspect and status.login:
        return []
    directory = status.home if isinstance(status, CodexStatus) else status.config_dir
    identity = directory / ("auth.json" if isinstance(status, CodexStatus) else ".claude.json")
    for problem in status.problems:
        if any(problem.startswith(f"accounts: {path} {reason}")
               for path in (str(directory), status.folder, str(identity))
               for reason in ("is missing", "is not a directory", "is a link", "is not logged in")):
            return []
        if any(problem.startswith(f"accounts: could not {action} {path}:")
               for action in ("read", "inspect") for path in (str(directory), str(identity))):
            return []
    if not status.is_set_up:
        reason = "not set up: no folder holds this account"
    elif not status.account_is_safe_to_inspect:
        reason = "its folder could not be read"
    else:
        reason = "not logged in"
    return [f"* {status.label}: {reason}"]


def _status_notes(status: AccountStatus, *, detail: bool) -> list[str]:
    """What is wrong or unusual in one folder. Nothing for a folder in order."""
    notes = [*status.problems, *_account_state_notes(status)]
    if detail and status.unclassified:
        notes.append(f"{status.folder}: unclassified: {', '.join(status.unclassified)}")
    mcp_differences = [
        *(f"- {name}" for name in status.missing_mcp_servers),
        *(f"+ {name}" for name in status.extra_mcp_servers),
    ]
    if mcp_differences:
        notes.append(f"{status.folder}: mcpServers: {', '.join(mcp_differences)}")
    return notes


def _read_status(source: UsageSource, *, detail: bool) -> StatusReading:
    """One table row per account folder, then one line for each thing wrong in a folder.

    With `detail`, also unknown entries, each live session, the Codex workers and recent moves.
    """
    statuses = account_statuses()
    role_problems: list[str] = []
    try:
        selected_workers = role_account("workers")
    except AccountsError as error:
        role_problems.append(str(error))
        selected_workers = CodexWorkersAccount("", accounts_home() / ".codex")
    codex = codex_statuses(selected_workers.home)
    if not selected_workers.label:
        codex = [replace(status, role="spare") if status.role == CODEX_ROLE else status for status in codex]
    now = datetime.now(timezone.utc)
    usage = _stored_usage(statuses, codex, now, source)
    zone = agent_accounts.user_zone()
    try:
        groups = group_accounts()
    except AccountsError as error:
        if isinstance(source, RefreshStaleUsage):
            raise
        if str(error) not in role_problems:
            role_problems.append(str(error))
        groups = GroupAccounts()
        codex = [replace(status, role=NO_VALUE) for status in codex]
    group_directories: dict[ClaudeGroup, Path] = {}
    for group in CLAUDE_GROUPS:
        label = group_label(groups, group)
        if isinstance(label, GroupNotConfigured):
            role_problems.append(f"accounts: {group} has no account; run accounts.py move {group} <claudeN>")
        if isinstance(label, str) and _label_is(label, "claude"):
            try:
                group_directories[group] = _claude_role_selection(group, label).dir
            except AccountsError as error:
                role_problems.append(str(error))
                # The folder's status reports its unreadable note without selecting it.
                continue
    rows = (
        *(_status_row(status, usage.get(usage_store.key("Claude", status.login)), now,
            tuple(group for group, directory in group_directories.items() if status.config_dir == directory))
          for status in statuses),
        *(_status_row(status, usage.get(usage_store.key("Codex", status.login)), now,
            (status.role,) if status.role in (CODEX_ROLE, "spare") else ()) for status in codex),
    )
    home_step = MAC_HOME_LOGIN_STEP if sys.platform == "darwin" else "run accounts.py cutover-home"
    notes = [
        *(note for row in rows if (note := _read_failure_note(row, zone))),
        *role_problems,
        *([f"~/.claude holds a login; {home_step}"]
          if account.claude_account(main_config_dir()) is not None else []),
        *([f"~/.codex holds a login; run accounts.py cutover-home {home_account_label('codex')}"]
          if _codex_home_login(accounts_home() / ".codex") else []),
        *(note for status in statuses for note in _status_notes(status, detail=detail)),
        *(problem for status in codex for problem in status.problems),
        *(note for status in codex for note in _account_state_notes(status)),
        *(f"{status.folder}: unclassified: {', '.join(status.unclassified)}"
          for status in codex if detail and status.unclassified),
    ]
    detail_lines: tuple[str, ...] = ()
    if detail:
        journal = moves.read_journal()
        live_lines = ((*_live_status_lines(groups), *_workers_status_lines(now, selected_workers))
                      if isinstance(source, RefreshStaleUsage) else ())
        detail_lines = (*live_lines,
            *_move_status_lines(moves.account_moves(journal), zone), *_routing_status_lines(journal, zone))
    return StatusReading(rows, tuple(notes), detail_lines, zone, now)


@role_map.role_map_command
def read_status(*, source: UsageSource = REFRESH_STALE_USAGE, detail: bool = False) -> StatusReading:
    """Read each account once for either renderer; stored mode performs no live work."""
    with role_map.stored_role_map() if isinstance(source, StoredUsageOnly) else nullcontext():
        return _read_status(source, detail=detail)


def status_report(*, detail: bool = False, source: UsageSource = REFRESH_STALE_USAGE) -> str:
    """The account table, with optional session, worker and move details."""
    return render_status_table(read_status(source=source, detail=detail))


# Run folders under unit/, and under delegate/ for a run started before the rename.
WORKERS_RUN_ROOTS = unit_runs.run_roots()


def _live_status_lines(groups: GroupAccounts) -> list[str]:
    from scripts.message import sessions
    members = live_group_members()
    moving = {item.session_id: label for group in CLAUDE_GROUPS
              if isinstance(label := group_label(groups, group), str) for item in members[group]
              if not session_on_account(item, label)}
    lines: list[str] = []
    live = sessions.live_sessions()
    home_state = account.home_account_state()
    for session in live:
        setting = account.process_account_directory(account.process_config_dir(session["pid"]), home_state=home_state)
        directory = setting.path if isinstance(setting, account.ConfiguredClaudeFolder) else None
        identity = account_identity(directory) if directory is not None else agent_accounts.UnreadableIdentity(main_config_dir(), "account unreadable")
        label = identity.label if isinstance(identity, agent_accounts.NumberedAccount) else "account unreadable"
        suffix = f"; moving to {moving[session['sessionId']]}" if session["sessionId"] in moving else ""
        lines.append(f"session {session['sessionId']} ({session['name']}): {label}{suffix}")
    listed = {session["sessionId"] for session in live}
    for group in CLAUDE_GROUPS:
        for member in members[group]:
            if member.session_id not in listed:
                label = session_account_label(member)
                suffix = f"; moving to {moving[member.session_id]}" if member.session_id in moving else ""
                lines.append(f"session {member.session_id} ({member.name}): {label}{suffix}")
    return lines


def _mesh_server_running(run: Path) -> bool:
    try:
        raw = cast(object, json.loads((run / "mesh_server.json").read_text()))
        if not isinstance(raw, dict):
            return False
        pid = cast(dict[str, object], raw).get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _workers_status_lines(now: datetime, selected: CodexWorkersAccount) -> list[str]:
    exclude = frozenset({selected.label}) if _label_is(selected.label, "codex") else _NO_EXCLUSIONS
    chosen = pick("codex", now, exclude=exclude, keep=NoKeep())
    destination = chosen.label if isinstance(chosen, PickedAccount) else no_room_line("codex", chosen)
    lines = [f"Codex workers: {selected.label}; on refusal: {destination}"] if selected.label else []
    for path in sorted(path for root in WORKERS_RUN_ROOTS for path in root.glob("*/mesh_roster.json")):
        if not _mesh_server_running(path.parent):
            continue
        try:
            raw = cast(object, json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        for name, value in cast(dict[str, object], raw).items():
            if not isinstance(value, dict):
                continue
            entry = cast(dict[str, object], value)
            if entry.get("continuation_owed") is True:
                reason = entry.get("continuation_reason")
                if not isinstance(reason, str):
                    reason = "quota refusal; waiting for account recovery or a confirmed move"
                    if entry.get("status") == "waiting_capacity":
                        reason = "waiting for capacity on the selected account"
                lines.append(f"worker {path.parent.name}/{name}: continuation owed — {reason}")
    return lines


def _move_status_lines(entries: list[moves.MoveEntry], zone: tzinfo) -> list[str]:
    lines: list[str] = []
    for entry in entries[-10:]:
        at = datetime.fromisoformat(entry["at"]).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")
        detail: str = entry["kind"]
        if entry["kind"] == "requested":
            outcomes: list[str] = []
            for outcome in entries:
                if outcome["move_id"] != entry["move_id"] or outcome["subject"] != entry["subject"]:
                    continue
                if outcome["kind"] == "confirmed":
                    worker = outcome.get("worker", entry["subject"])
                    outcomes.append(f"confirmed {worker} on {outcome.get('home', outcome.get('config_dir', ''))}")
                elif outcome["kind"] == "failed":
                    outcomes.append("failed: " + outcome["reason"])
            cause = f" ({entry.get('cause', 'refusal')})"
            detail = f"requested {entry['from']} → {entry['to']}: {entry['why']}{cause} — " + ("; ".join(outcomes) or "pending")
        elif entry["kind"] == "confirmed":
            detail = f"confirmed {entry.get('worker', entry['subject'])}: {entry.get('home', entry.get('config_dir', ''))}"
        elif entry["kind"] == "failed":
            detail = "failed: " + entry["reason"]
        elif entry["kind"] == "waited":
            detail = "waited until " + datetime.fromisoformat(entry["reset"]).astimezone(zone).isoformat()
        elif entry["kind"] == "no room":
            reset = entry["earliest_reset"]
            detail = "no room until " + (datetime.fromisoformat(reset).astimezone(zone).isoformat() if reset else "unknown reset")
        elif entry["kind"] == "room again":
            detail = "room again: " + str(entry["label"])
        lines.append(f"{at} {entry['tool']} {entry['subject']}: {detail}")
    return lines


def _routing_status_lines(entries: list[moves.JournalEntry], zone: tzinfo) -> list[str]:
    routings: dict[str, list[moves.RoutingEntry]] = {}
    for entry in entries:
        match entry["kind"]:
            case "routed to claude" | "routed back" | "routing not restored":
                routings.setdefault(entry["fallback_at"], []).append(entry)
            case "requested" | "confirmed" | "failed" | "waited" | "wait ended" | "no room" | "room again":
                continue
    lines: list[str] = []
    for fallback_at, trail in routings.items():
        opening = next((entry for entry in trail if entry["kind"] == "routed to claude"), None)
        owner = trail[0]["owner"]
        started = datetime.fromisoformat(fallback_at).astimezone(zone).isoformat()
        if opening is not None:
            text = f"routed to claude {started} ({owner}, {opening['cause']}): {', '.join(opening['moved'])}"
        else:
            text = f"routed to claude {started} ({owner}; opening entry not recorded)"
        ending = next((entry for entry in reversed(trail) if entry["kind"] != "routed to claude"), None)
        if ending is None:
            text += " — open"
        else:
            at = datetime.fromisoformat(ending["at"]).astimezone(zone).isoformat()
            if ending["kind"] == "routed back":
                text += f" — routed back {at}"
            else:
                text += f" — not restored ({ending['outcome']}) {at}: {', '.join(ending['failed'])}"
        lines.append(text)
    return ["Codex work routing:", *lines] if lines else []


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action")
    _ = commands.add_parser("role-map-read")
    writer = commands.add_parser("role-map-write")
    _ = writer.add_argument("change")
    _ = commands.add_parser("next-read")
    next_writer = commands.add_parser("next-write")
    _ = next_writer.add_argument("entries")
    def insertion(text: str) -> next_moves.InsertAt:
        return next_moves.InsertAt(next_moves.GroupPosition(int(text)))
    _ = next_writer.add_argument("--at", dest="insertion", type=insertion, default=next_moves.AppendToGroup())
    next_command = commands.add_parser("next")
    next_verbs = next_command.add_subparsers(dest="next_action", required=True)
    next_add = next_verbs.add_parser("add")
    _ = next_add.add_argument("group", choices=(*CLAUDE_GROUPS, "workers"))
    _ = next_add.add_argument("percent", type=float)
    _ = next_add.add_argument("label")
    hold_options = next_add.add_mutually_exclusive_group()
    _ = hold_options.add_argument("--hold-minutes", dest="hold_mode",
                                  type=lambda text: next_moves.FixedHold(float(text)), default=next_moves.FixedHold(0))
    def half_point_hold(text: str) -> next_moves.HalfPointHold:
        if text != "half-point":
            raise argparse.ArgumentTypeError("hold must be half-point")
        return next_moves.HalfPointHold()
    _ = hold_options.add_argument("--hold", dest="hold_mode", type=half_point_hold)
    _ = next_add.add_argument("--at", dest="insertion", type=insertion, default=next_moves.AppendToGroup())
    _ = next_verbs.add_parser("list")
    next_remove = next_verbs.add_parser("remove")
    _ = next_remove.add_argument("group", choices=(*CLAUDE_GROUPS, "workers"))
    def position(text: str) -> next_moves.GroupPosition:
        return next_moves.GroupPosition(int(text))
    _ = next_remove.add_argument("next_position", type=position)
    next_move = next_verbs.add_parser("move")
    _ = next_move.add_argument("group", choices=(*CLAUDE_GROUPS, "workers"))
    _ = next_move.add_argument("next_position", type=position)
    _ = next_move.add_argument("next_destination", type=position)
    next_hold = next_verbs.add_parser("hold")
    _ = next_hold.add_argument("group", choices=(*CLAUDE_GROUPS, "workers"))
    _ = next_hold.add_argument("next_position", type=position)
    _ = next_hold.add_argument("hold_mode", type=next_moves.parse_hold_mode)
    _ = next_hold.add_argument("--expect", dest="next_expect", nargs=2, default=[], metavar=("PERCENT", "TARGET"))
    parser.set_defaults(action="status", as_json=False, stored=False)
    _ = parser.add_argument("--detail", action="store_true")
    for action in ("setup", "check"):
        command = commands.add_parser(action)
        _ = command.add_argument("label")
    cutover = commands.add_parser("cutover-home")
    _ = cutover.add_argument("label", nargs="?", default="")
    _ = cutover.add_argument("--undo", action="store_true")
    status = commands.add_parser("status")
    _ = status.add_argument("--json", dest="as_json", action="store_true")
    _ = status.add_argument("--stored", action="store_true")
    _ = status.add_argument("--detail", action="store_true", default=argparse.SUPPRESS)
    placer = commands.add_parser("placement")
    _ = placer.add_argument("--explain", action="store_true")
    exec_command = commands.add_parser("exec")
    _ = exec_command.add_argument("--probe", action="store_true", help="mark the session as a throwaway probe: no shutdown, restart or move for it ever pushes to the phone")
    _ = exec_command.add_argument("label")
    _ = exec_command.add_argument("claude_args", nargs=argparse.REMAINDER)
    move_command = commands.add_parser("move")
    _ = move_command.add_argument("session")
    _ = move_command.add_argument("label", nargs="?", default="")
    _ = move_command.add_argument("--from", dest="source", default="")
    _ = move_command.add_argument("--to", dest="target", default="")
    _ = move_command.add_argument("--dry-run", action="store_true")
    _ = move_command.add_argument("--no-alert", dest="alerts", action="store_const", const="none", default="user")
    _ = move_command.add_argument("--why", default="")
    for action in ("role", "env"):
        command = commands.add_parser(action)
        _ = command.add_argument("role")
    set_role_command = commands.add_parser("set-role")
    _ = set_role_command.add_argument("role")
    _ = set_role_command.add_argument("label")
    swap_command = commands.add_parser("swap")
    _ = swap_command.add_argument("role")
    _ = swap_command.add_argument("label")
    _ = swap_command.add_argument("--dry-run", action="store_true")
    burn_command = commands.add_parser("burn")
    _ = burn_command.add_argument("--since", required=True)
    _ = burn_command.add_argument("--until", default="")
    return parser


def group_half_point_hold(group: ClaudeGroup, now: datetime) -> next_moves.ResolvedHold:
    label = group_label(group_accounts(), group)
    return (next_moves.resolve_hold(next_moves.HalfPointHold(), label, now) if isinstance(label, str)
            else next_moves.ResolvedHold(40, next_moves.NoUsablePace()))


def validate_next_move_target(entry: next_moves.NextMove) -> None:
    if isinstance(entry, next_moves.ClaudeGroupMove):
        _ = claude_account_dir(entry.to)
    elif isinstance(entry.to, next_moves.CodexWorkersTarget):
        _ = check_codex_target(entry.to.label, accounts_home() / ".codex", repair=False)
    elif isinstance(entry.to.account, next_moves.ClaudeWorkersTarget):
        _ = claude_workers_account(entry.to.account.label)


@role_map.role_map_command
def main(argv: list[str]) -> int:
    parser = _parser()
    options = cast(CommandLine, parser.parse_args(argv))
    if options.as_json and options.detail:
        parser.error("--json and --detail cannot be used together")
    if hasattr(options, "label"):
        options.label = note_file.canonical_label(options.label)
    try:
        if options.action in ("next-read", "next-write"):
            if not role_map.is_role_map_host():
                raise AccountsError("accounts: account moves belong on natedev")
            if options.action == "next-read":
                print(next_moves.encode_next_moves(next_moves.read_next_moves()))
            else:
                entries = next_moves.decode_next_moves(options.entries)
                if len(entries) != 1:
                    raise AccountsError("accounts: next-write requires one entry")
                entry = entries[0]
                validate_next_move_target(entry)
                next_moves.insert_next_move(entry, options.insertion)
                print(next_moves.encode_next_moves(entries))
            return 0
        if options.action == "next":
            if options.next_action == "add":
                if role_map.is_role_map_host():
                    candidate = next_moves.decode_next_moves(json.dumps([dict(id="validation", group=options.group,
                        at_percent=options.percent, **next_moves.encode_hold_mode(options.hold_mode), to=options.label,
                        added_at=datetime.now(timezone.utc).isoformat())]))[0]
                    validate_next_move_target(candidate)
                _ = next_moves.add_next_move(options.group, options.percent, options.label,
                                             options.hold_mode, insertion=options.insertion)
                print(next_moves.list_next_moves() if role_map.is_role_map_host()
                      else f"added {options.group} account move")
            elif options.next_action == "remove":
                next_moves.remove_next_move(options.group, options.next_position)
                print(f"removed {options.group} {options.next_position.value}")
            elif options.next_action == "move":
                next_moves.move_next_move(options.group, options.next_position, options.next_destination)
                print(f"moved {options.group} {options.next_position.value} to {options.next_destination.value}")
            elif options.next_action == "hold":
                expected: next_moves.ExpectedNextMove | next_moves.UncheckedNextMove = next_moves.UncheckedNextMove()
                if options.next_expect:
                    try:
                        percent = float(options.next_expect[0])
                    except ValueError as error:
                        raise next_moves.NextMoveError("next: expected percent must be from 0 to 100") from error
                    if not math.isfinite(percent) or not 0 <= percent <= 100:
                        raise next_moves.NextMoveError("next: expected percent must be from 0 to 100")
                    expected = next_moves.ExpectedNextMove(percent, options.next_expect[1])
                print(next_moves.update_next_move_hold(options.group, options.next_position, options.hold_mode, expected))
            else:
                print(next_moves.list_next_moves())
            return 0
        if options.action in ("role-map-read", "role-map-write"):
            if not role_map.is_role_map_host():
                raise AccountsError(f"accounts: role map belongs on {role_map.ROLE_MAP_HOST}")
            if options.action == "role-map-read":
                print(role_map.encode_role_map(read_role_map()))
            else:
                write_role_map(role_map.decode_role_map_change(options.change))
            return 0
        if options.action == "cutover-home":
            try:
                cutover_home(options.label, undo=bool(getattr(options, "undo", False)))
            except AccountsError:
                raise
            except (OSError, ValueError) as error:
                raise AccountsError(f"accounts: cutover-home: {error}") from error
            return 0
        if options.detail and options.action != "status":
            raise AccountsError("--detail goes only with no action")
        if options.action == "placement":
            print(placement_report())
            return 0
        if options.action == "exec":
            exec_account(options.label, options.claude_args, probe=options.probe)
            return 0
        if options.action == "move":
            if options.session in CLAUDE_GROUPS:
                if not options.label or options.source or options.target:
                    raise AccountsError("move units|showrunners requires <claudeN>")
                moved = move_group(options.session, options.label, dry_run=options.dry_run, why=options.why, alerts=options.alerts)
            else:
                if not options.label or options.source or options.target:
                    raise AccountsError("move requires <session-id|name> <claudeN>")
                moved = move(options.session, options.label, dry_run=options.dry_run, why=options.why, alerts=options.alerts)
            if isinstance(moved, MoveRefused):
                print(moved.reason, file=sys.stderr)
                return 1
            if isinstance(moved, (GroupMoveDryRun, MoveDryRun)):
                print("\n".join(moved.lines))
                return 0
            _ = sys.stdout.write(moved.stdout)
            _ = sys.stderr.write(moved.stderr)
            return moved.returncode
        if options.action == "setup":
            problems = setup(options.label)
            for problem in problems:
                print(problem, file=sys.stderr)
            if _label_is(options.label, "codex"):
                home = codex_setup_home(options.label)
                if not problems and home.is_dir() and not home.is_symlink():
                    print(f"CODEX_HOME=~/{home.name} codex login")
                    print("Keep CODEX_HOME on that command: codex login without it signs ~/.codex out and in; workers always use their mapped account.")
                return 1 if problems else 0
            destination = account_dir(options.label)
            print(f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={shlex.quote(str(destination))} claude")
            print("/login")
            return 1 if problems else 0
        if options.action == "check":
            problems = check(options.label)
            for problem in problems:
                print(problem)
            return 1 if problems else 0
        if options.action == "status":
            source: UsageSource = StoredUsageOnly() if options.stored else RefreshStaleUsage()
            if options.as_json:
                print(render_status_json(read_status(source=source)))
            else:
                print(status_report(detail=options.detail, source=source))
            return 0
        if options.action == "role":
            selected = role_account(options.role)
            print(selected.label)
            return 0
        if options.action == "env":
            print(launch_prefix(options.role))
            return 0
        if options.action == "set-role":
            set_role(options.role, options.label)
            return 0
        if options.action == "swap":
            result = swap(options.role, options.label, dry_run=options.dry_run)
            if isinstance(result, (WorkersSwapDryRun, WorkersSwapStarted)):
                print("\n".join(result.lines))
                return 0
            if isinstance(result, SwapDryRun):
                print(shlex.join(result.role_command))
                print(shlex.join(result.down_command))
                return 0
            _ = sys.stdout.write(result.stdout)
            _ = sys.stderr.write(result.stderr)
            if result.stdout and not result.stdout.endswith("\n"):
                print()
            if isinstance(result, SwapRolledBack):
                print(f"unit account unchanged: still {result.old_label}")
                return result.returncode
            print(f"units come back on {result.target_label} when {result.old_label} is down")
            return 0
        if options.action == "burn":
            since = _iso_time(options.since, "--since")
            until = _iso_time(options.until, "--until") if options.until else datetime.now(timezone.utc)
            results = burn(since, until)
            for result in results:
                roles = f" ({', '.join(result.roles)})" if result.roles else ""
                print(
                    f"{result.label}{roles}: {result.points_per_day:g} points/day "
                    + f"({result.points:g} points over {result.observed_days:g} observed days)"
                )
            if not results:
                print("No paired quota readings in this window.")
            return 0
        raise AccountsError(f"accounts: unknown action {options.action}")
    except LaunchShellError as error:
        print(error, file=sys.stderr)
        return 1
    except (AccountsError, next_moves.NextMoveError, role_map.RoleMapUnavailable, role_map.RoleMapWriteRefused) as error:
        print(error, file=sys.stderr)
        return 2 if options.action == "exec" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
