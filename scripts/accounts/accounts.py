#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Build and inspect Claude and Codex account directories; select Claude roles."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import asyncio
import ctypes
import errno
import fcntl
import io
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
import tomllib
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import Context, ContextVar
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, nullcontext, redirect_stderr
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import override, TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, TextIO, cast, overload
from importlib import import_module


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"
# Direct CLI and imported callers share the picker's domain variants.
_ = sys.modules.setdefault("scripts.accounts.accounts", sys.modules[__name__])

from scripts.whoami import account  # noqa: E402
from scripts.herdr import herdr_types  # noqa: E402
from scripts.whoami.account import ResolvedHistoryLabel as ResolvedHistoryLabel, HistoricalAccountUnresolved as HistoricalAccountUnresolved  # noqa: E402
from scripts.lib import atomic_file, conf, file_lock, models_registry  # noqa: E402
from scripts.lib.agent_program import resolve_program as resolve_program
from scripts.lib.probe_session import PROBE_ENV  # noqa: E402
from scripts.whoami import agent_accounts  # noqa: E402
from scripts.whoami import note_file  # noqa: E402
from scripts.whoami import usage_store  # noqa: E402
from scripts.accounts import refusals  # noqa: E402
from scripts.accounts import moves, worker_names, role_map, session_state  # noqa: E402
from scripts.accounts.refusals import (  # noqa: E402
    KnownReset as KnownReset, NoResetKnown as NoResetKnown,
    ReadingWindow as ReadingWindow, NoWindow as NoWindow, FreshReading as FreshReading,
    StaleReading as StaleReading, NoReading as NoReading,
)

from scripts.accounts import next_moves
from scripts.agents import remote, stop_results
from scripts.accounts.role_map import (  # noqa: E402
    RoleMap as RoleMap, RoleMapChange as RoleMapChange,
    StoredGroupSelection as StoredGroupSelection, StoredGroupSelections as StoredGroupSelections,
    TransitionReason as TransitionReason, RecordedTransitionReason as RecordedTransitionReason,
    UnavailableTransitionReason as UnavailableTransitionReason,
)

if TYPE_CHECKING:
    from scripts.agents.inventory import ListedSession, Session
    from scripts.production import staff


MoveAlerts = Literal["user", "none"]


@dataclass(frozen=True)
class ReadCurrentAccountSettings:
    """The caller has not read account settings for this pass."""


CURRENT_ACCOUNT_SETTINGS = ReadCurrentAccountSettings()


def read_account_settings(
    configured: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS,
) -> conf.ConfigRead:
    """Share the caller's settings or read them for this pass."""
    return conf.read("accounts") if isinstance(configured, ReadCurrentAccountSettings) else configured


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
class NoObservedStateCause:
    """No direct directory or identity fault explains the account state."""


@dataclass(frozen=True)
class ObservedStateCause:
    """The inspection's direct diagnostic and its complete cause identity."""

    diagnostic: str
    key: ProblemCauseKey


NO_OBSERVED_STATE_CAUSE = NoObservedStateCause()


@dataclass(frozen=True)
class AccountDirectoryReady:
    """A safe directory logged in as the account its note names."""

    login: str
    problems: tuple[str, ...] = ()
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    note_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)


@dataclass(frozen=True)
class AccountDirectoryLoggedOut:
    """A safe directory that can start Claude to log in."""

    problems: tuple[str, ...]
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    note_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)


@dataclass(frozen=True)
class AccountDirectoryWrongLogin:
    """A readable directory logged in as a different account."""

    login: str
    problems: tuple[str, ...]
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    note_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)


@dataclass(frozen=True)
class AccountDirectoryNotSetUp:
    """The named directory does not exist yet."""

    problems: tuple[str, ...]
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    note_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)


@dataclass(frozen=True)
class AccountDirectoryBrokenLinks:
    """Directory or identity isolation, or a shared link, needs repair."""

    problems: tuple[str, ...]
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    note_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)


@dataclass(frozen=True)
class AccountDirectoryUnreadable:
    """Identity or required account-note data cannot establish readiness."""

    problems: tuple[str, ...]
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    note_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)


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
    cause_kind: Literal["access", "identity"] = "access"


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
    """A launch refusal can retain its observed cause for status."""

    def __init__(self, message: str, *, source: ObservedStateCause | NoObservedStateCause = NO_OBSERVED_STATE_CAUSE) -> None:
        super().__init__(message)
        self.source: ObservedStateCause | NoObservedStateCause = source


class WorkerMovesStartFailed(AccountsError):
    """The detached worker runner did not start; its request remains pending."""


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
    mcp_differences: McpServerDifferences
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    note_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    mcp_baseline_problems: tuple[UnreadableMcpConfiguration, ...] = ()


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

    note_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)

class CommandLine(argparse.Namespace):
    next_action: str = ""
    group: next_moves.NextMoveGroup = "units"
    percent: float = 0
    hold_mode: next_moves.HoldMode = next_moves.FixedHold(0)
    fallback_targets: list[str] = []
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
    follow_role: bool = False
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
    server: str = ""
    direction: McpDirection = "off"
    counts_lock: int = -1


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
    """Each worker has a completed, pending or failed move outcome."""

    outcomes: tuple[SessionMoveOutcome, ...]

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(move_result_line(result) for result in self.outcomes)

    @property
    def returncode(self) -> int:
        return _move_output(self.outcomes, ())[2]


WorkersSwapOutcome = WorkersSwapDryRun | WorkersSwapStarted


@dataclass(frozen=True)
class SelectableCodexAccount:
    """The selected home and its last read quota key."""

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


def claude_config_label(config_dir: Path) -> str:
    """The launch folder's account label, retaining legacy and custom folder names.

    This names a folder without reading its login or validating it as a numbered account.
    """
    return config_dir.name.removeprefix(".")


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
        else:
            rename_exclusive = cast(Callable[[bytes, bytes, int], int], library.renamex_np)
            result = rename_exclusive(os.fsencode(source), os.fsencode(destination), RENAME_EXCL)
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
    if destination == source_dir:
        return []
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


def read_json_object(path: Path | io.StringIO) -> dict[str, object]:
    decoded = cast(object, json.loads(path.read_text(encoding="utf-8") if isinstance(path, Path) else path.read()))
    if not isinstance(decoded, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, object], decoded)


def trust_settings_file() -> Path:
    return REPOSITORY_ROOT / "config" / "accounts.toml"


def _accounts_settings_section(document: dict[str, object], name: str) -> dict[str, object]:
    section = document.get(name)
    if not isinstance(section, dict):
        print(f"accounts: anomaly: {trust_settings_file()}: expected [{name}] section", file=sys.stderr)
        return {}
    return cast(dict[str, object], section)


def _trusted_folders(document: dict[str, object]) -> tuple[str, ...]:
    """Resolve the configured project paths for this machine."""
    folders = _accounts_settings_section(document, "trust").get("folders", [])
    if not isinstance(folders, list):
        print("accounts: anomaly: trust.folders must be a list", file=sys.stderr)
        return ()
    result: list[str] = []
    for folder in cast(list[object], folders):
        if isinstance(folder, str) and Path(folder).expanduser().is_absolute():
            result.append(str(Path(folder).expanduser().absolute()))
        else:
            print("accounts: anomaly: trust folder is not an absolute path", file=sys.stderr)
    return tuple(dict.fromkeys(result))


def with_trusted_folders(source: dict[str, object], folders: tuple[str, ...]) -> dict[str, object]:
    """Change only the configured projects' trust bit; seed and repair share it."""
    if not folders:
        return source
    raw_projects = source.get("projects", {})
    if not isinstance(raw_projects, dict):
        raise ValueError("projects must be an object")
    projects = dict(cast(dict[str, object], raw_projects))
    for folder in folders:
        raw_project = projects.get(folder, {})
        if not isinstance(raw_project, dict):
            raise ValueError("configured trust project must be an object")
        projects[folder] = {**cast(dict[str, object], raw_project), "hasTrustDialogAccepted": True}
    return {**source, "projects": projects}


def without_project_entries(source: dict[str, object], folders: tuple[str, ...]) -> dict[str, object]:
    """Remove project entries for the exact folder keys. Preserve all other entries and settings."""
    if not folders:
        return source
    raw_projects = source.get("projects", {})
    if not isinstance(raw_projects, dict):
        raise ValueError("projects must be an object")
    projects = cast(dict[str, object], raw_projects)
    remaining = {folder: project for folder, project in projects.items() if folder not in folders}
    return {**source, "projects": remaining} if len(remaining) != len(projects) else source


@contextmanager
def _account_home_lock(home: Path) -> Generator[None]:
    """Serialize account-file publication with setup's directory flock."""
    directory = os.open(home, os.O_RDONLY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        yield
    finally:
        os.close(directory)


@dataclass(frozen=True)
class TrustConfiguration:
    """Configured folders for one trust or seed operation."""

    folders: tuple[str, ...]


def trust_configuration() -> TrustConfiguration:
    """Read and validate all trust settings before any home mutation or lock."""
    path = trust_settings_file()
    try:
        document = cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise AccountsError(f"accounts: {path}: {error}") from error
    return TrustConfiguration(_trusted_folders(document))


def _change_trust_file(home: Path, path: Path,
                       change: Callable[[dict[str, object]], dict[str, object]]) -> bool:
    with _account_home_lock(home), file_lock.claude_config_lock(path):
        while True:
            before_mode = stat.S_IMODE(path.stat().st_mode)
            before = path.read_bytes()
            original = read_json_object(io.StringIO(before.decode("utf-8")))
            updated = change(original)
            if updated == original:
                return False
            published = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
            with atomic_file.prepared_replacement(path, published, mode=before_mode) as temporary:
                if path.read_bytes() != before or stat.S_IMODE(path.stat().st_mode) != before_mode:
                    continue
                os.replace(temporary, path)
                atomic_file.sync_folder(path.parent)
                if path.read_bytes() == published.encode("utf-8"):
                    return True
                # Preserve changes from writers that do not use the lock.


def _trust_file(home: Path, path: Path, configuration: TrustConfiguration) -> bool:
    return _change_trust_file(home, path, lambda source: with_trusted_folders(source, configuration.folders))


@dataclass(frozen=True)
class AccountTrustFile:
    """The trust file and account home used for locked publication."""

    label: str
    home: Path
    path: Path


def _account_trust_files() -> list[AccountTrustFile]:
    homes = [NamedAccount("~/.claude", main_config_dir()), *_existing_named_accounts()]
    return [AccountTrustFile(home.label, home.dir,
                            accounts_home() / ".claude.json" if home.dir == main_config_dir()
                            else home.dir / ".claude.json") for home in homes]


def source_has_claude_trust(source: Path) -> bool:
    """Return whether any account trusts the source through the shared Claude trust check."""
    from scripts.herdr import herdr

    try:
        files = _account_trust_files()
    except (OSError, ValueError) as error:
        print(f"accounts: anomaly: cannot list trust files ({type(error).__name__})", file=sys.stderr)
        return False
    for account_file in files:
        try:
            if isinstance(herdr.claude_trust(source, account_file.path), herdr.Trusted):
                return True
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            print(f"accounts: anomaly: {account_file.path}: cannot check trust ({type(error).__name__})", file=sys.stderr)
    return False


def set_claude_folder_trust(folder: Path, *, trusted: bool) -> None:
    """Trust a folder or remove its project entry in every existing account file. Report each failed home independently."""
    try:
        key = str(folder.resolve())
        files = _account_trust_files()
    except (OSError, ValueError) as error:
        print(f"accounts: anomaly: cannot prepare folder trust ({type(error).__name__})", file=sys.stderr)
        return
    change = with_trusted_folders if trusted else without_project_entries
    for account_file in files:
        try:
            _ = account_file.path.stat()
            _ = _change_trust_file(account_file.home, account_file.path, lambda source: change(source, (key,)))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            print(f"accounts: anomaly: {account_file.path}: cannot change trust ({type(error).__name__})", file=sys.stderr)


def trust_accounts() -> int:
    """Apply configured trust to each home independently without inspecting identity."""
    configuration = trust_configuration()
    failures = False
    for account_file in _account_trust_files():
        try:
            changed = _trust_file(account_file.home, account_file.path, configuration)
            state = "updated" if changed else "unchanged"
            print(f"{account_file.label}: trust {state}")
        except (OSError, ValueError) as error:
            print(f"{account_file.label}: trust failed: {error}", file=sys.stderr)
            failures = True
    return int(failures)


def _filtered_seed(source: dict[str, object], folders: tuple[str, ...]) -> ClaudeConfigSeed:
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
    return cast(ClaudeConfigSeed, cast(object, with_trusted_folders(seed, folders)))


def _seed_config(destination: Path, configuration: TrustConfiguration) -> list[str]:
    source = accounts_home() / ".claude.json"
    try:
        with _account_home_lock(destination.parent):
            atomic_file.remove_dead_temporary_files(destination)
            if destination.exists():
                existing = destination.lstat()
                if not stat.S_ISREG(existing.st_mode) or existing.st_size:
                    return []
                destination.unlink()
            seed = _filtered_seed(read_json_object(source), configuration.folders)
            _ = atomic_file.create_text_atomic(destination, json.dumps(seed, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, TypeError) as error:
        return [f"accounts: could not seed {destination} from {source}: {error}"]
    return []


def setup(label: str) -> list[str]:
    """Build or repair one named account, returning every refusal or failure."""
    label = note_file.canonical_label(label)
    if _label_is(label, "codex"):
        return _setup_codex_home(label)
    configuration = trust_configuration()
    destination = account_dir(label)
    problems: list[str] = []
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

    problems.extend(_seed_config(destination / ".claude.json", configuration))
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
        values = read_json_object(path)
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
    return UnreadableClaudeDirectoryIdentity(f"accounts: {path} has an invalid oauthAccount identity", "identity")


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


def _note_source(label: str, expected: MissingAccountNote | UnreadableAccountNote) -> ObservedStateCause:
    return ObservedStateCause(expected.problem, ProblemCauseKey(
        "note" if isinstance(expected, MissingAccountNote) else "unreadable note",
        CauseAccount(label), CauseFile(str(note_file.note_path(label)))))


def _private_identity_problems(destination: Path) -> list[str]:
    problems: list[str] = []
    for name in (".credentials.json", "account-label"):
        path = destination / name
        if path.is_symlink():
            continue
        try:
            if name == ".credentials.json":
                _ = read_json_object(path)
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
    expected = _account_note(label)
    return replace(_inspect_account_directory(selected, expected), note_source=(
        _note_source(label, expected) if isinstance(expected, (MissingAccountNote, UnreadableAccountNote))
        else NO_OBSERVED_STATE_CAUSE))


def _observed_state_cause(selected: NamedAccount, kind: ProblemKind, path: Path,
                          diagnostic: str) -> ObservedStateCause:
    return ObservedStateCause(diagnostic, ProblemCauseKey(kind, CauseAccount(selected.label), CauseFile(str(path))))


def _inspect_account_directory(
    selected: NamedAccount, expected: AccountNote, *, allow_shared_repair: bool = False,
) -> AccountDirectoryReadiness:
    label = selected.label
    destination = selected.dir
    if destination.is_symlink():
        diagnostic = f"accounts: {destination} is a link; an account dir must be a real directory"
        return AccountDirectoryBrokenLinks((diagnostic,), _observed_state_cause(selected, "access", destination, diagnostic))
    try:
        metadata = destination.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            diagnostic = f"accounts: {destination} is not a directory"
            return AccountDirectoryBrokenLinks((diagnostic,), _observed_state_cause(selected, "access", destination, diagnostic))
    except FileNotFoundError:
        diagnostic = f"accounts: {destination} is missing"
        return AccountDirectoryNotSetUp((diagnostic,), _observed_state_cause(selected, "access", destination, diagnostic))
    except OSError as error:
        diagnostic = f"accounts: could not inspect {destination}: {error}"
        return AccountDirectoryUnreadable((diagnostic,), _observed_state_cause(selected, "access", destination, diagnostic))
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
        path = destination / ".claude.json"
        diagnostic = f"accounts: {path} is a link; identity stays in each account dir"
        return AccountDirectoryBrokenLinks(tuple(problems), _observed_state_cause(selected, "access", path, diagnostic))
    state_source: ObservedStateCause | NoObservedStateCause = NO_OBSERVED_STATE_CAUSE
    identity = _directory_identity(destination)
    if isinstance(identity, UnreadableClaudeDirectoryIdentity):
        problems.append(identity.problem)
        state_source = _observed_state_cause(selected, identity.cause_kind,
            agent_accounts.claude_config_file(destination), identity.problem)
    if isinstance(expected, UnreadableAccountNote):
        problems.append(expected.problem)
    if isinstance(identity, ClaudeDirectoryLoggedOut):
        diagnostic = f"accounts: {destination} is not logged in"
        problems.append(diagnostic)
        state_source = _observed_state_cause(selected, "signed out", destination, diagnostic)
    if isinstance(expected, MissingAccountNote):
        problems.append(expected.problem)
    wrong_login = False
    if isinstance(identity, ClaudeDirectoryLogin) and isinstance(expected, ExpectedAccountLogin):
        wrong_login = identity.login.casefold() != expected.login.casefold()
        if wrong_login:
            problems.append(
                f"accounts: ~/.claude{_label_number(label)} holds a different login from {label}; "
                + f"run {_claude_login_command(destination)}, then /login as {label}"
            )
    if links:
        return AccountDirectoryBrokenLinks(tuple(problems), state_source)
    if (
        isinstance(identity, UnreadableClaudeDirectoryIdentity)
        or isinstance(expected, UnreadableAccountNote)
        or private_problems
        or isinstance(identity, ClaudeDirectoryLogin) and isinstance(expected, MissingAccountNote)
    ):
        return AccountDirectoryUnreadable(tuple(problems), state_source)
    if isinstance(identity, ClaudeDirectoryLoggedOut):
        return AccountDirectoryLoggedOut(tuple(problems), state_source)
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
    try:
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.chmod(0o700)
    except OSError as error:
        return [f"accounts: could not create {destination}: {error}"]
    problems = _repair_shared_links(
        destination, (accounts_home() / ".codex").absolute(), CODEX_SHARED, CODEX_ALWAYS_LINKED,
    )
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
    return list(_inspect_codex_home(label, _codex_account_home(label), workers_home,
        allow_shared_repair=allow_shared_repair).problems)


@dataclass(frozen=True)
class CodexHomeInspection:
    """Home checks and the observed fault that explains its account state."""

    problems: tuple[str, ...]
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)


def _inspect_codex_home(label: str, destination: Path, workers_home: Path, *, allow_shared_repair: bool = False) -> CodexHomeInspection:
    folder = _codex_folder(destination)
    if destination.is_symlink():
        diagnostic = f"accounts: {folder} is a link; an account home must be a real directory"
        return CodexHomeInspection((diagnostic,), ObservedStateCause(diagnostic,
            ProblemCauseKey("access", CauseAccount(label), CauseFile(str(destination)))))
    if not destination.exists():
        diagnostic = f"accounts: {folder} is missing"
        return CodexHomeInspection((diagnostic,), ObservedStateCause(diagnostic,
            ProblemCauseKey("access", CauseAccount(label), CauseFile(str(destination)))))
    if not destination.is_dir():
        diagnostic = f"accounts: {folder} is not a directory"
        return CodexHomeInspection((diagnostic,), ObservedStateCause(diagnostic,
            ProblemCauseKey("access", CauseAccount(label), CauseFile(str(destination)))))
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
    return CodexHomeInspection(tuple(problems))


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
    """The launch account could not be resolved."""
    reason: str


def prepared_account(label: str, *, resume: bool) -> PreparedAccount | AccountRefused:
    """Repair and prepare a numbered account for an actual launch."""
    return _prepared_account(label, resume=resume, repair=True)


def prepared_account_preview(label: str, *, resume: bool) -> PreparedAccount | AccountRefused:
    """Judge a launch after possible repairs without changing the account."""
    return _prepared_account(label, resume=resume, repair=False)


def _prepared_account(label: str, *, resume: bool, repair: bool) -> PreparedAccount | AccountRefused:
    del resume
    label = note_file.canonical_label(label)
    try:
        refuse_default(label)
        if label in ("unit", "showrunner", "units", "showrunners"):
            selected = role_account("unit" if label in ("unit", "units") else "showrunner")
        else:
            parsed = numbered_account_label(label, "claude")
            selected = NamedAccount(parsed.label, account_dir(parsed.label))
    except AccountsError as error:
        return AccountRefused(str(error))
    if repair:
        for problem in _repair_shared_links(selected.dir, main_config_dir().absolute(), SHARED, ALWAYS_LINKED):
            account_anomaly(problem)
    return PreparedAccount(account.named_account_environment(selected.dir), frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))


def account_anomaly(reason: str) -> None:
    """Report an anomaly without stopping the operation."""
    print("accounts: anomaly: " + reason.removeprefix("accounts: ").splitlines()[0], file=sys.stderr)


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
        account_anomaly(result.reason)
        return base_env
    if isinstance(result, (loader.Loaded, loader.NoEnvrc)):
        environment = dict(result.env)
        # The folder's shell must not change the launcher's account home.
        if "HOME" in base_env:
            environment["HOME"] = base_env["HOME"]
        else:
            _ = environment.pop("HOME", None)
        return environment
    account_anomaly("nix shell helper returned an unsupported result")
    return base_env


def _update_codex(arguments: list[str], *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> int:
    """Update both shared installs and report one result for each machine."""
    account_config = read_account_settings(account_config)
    environment = launch_shell_environment(Path.cwd())
    environment["CODEX_HOME"] = str(accounts_home() / ".codex")
    machine = remote.local_machine()
    local_ok = False
    try:
        program = resolve_program("codex", environment=environment)
        updated = subprocess.run([program, *arguments], env=environment, check=False)
        if updated.returncode:
            print(f"{machine}: update failed (exit {updated.returncode})", flush=True)
        else:
            version = subprocess.run([program, "--version"], env=environment,
                capture_output=True, text=True, timeout=account_config.whole("codex_version_timeout_seconds"), check=False)
            local_ok = version.returncode == 0 and bool(version.stdout.strip())
            detail = version.stdout.strip().splitlines()[-1] if local_ok else f"version read failed (exit {version.returncode})"
            print(f"{machine}: {detail}", flush=True)
    except subprocess.TimeoutExpired:
        print(f"{machine}: version read timed out", flush=True)
    except OSError:
        print(f"{machine}: codex could not start", flush=True)

    machine = remote.other_machine()
    command = 'CODEX_HOME="$HOME/.codex" "$HOME/.local/bin/codex" update >&2 && "$HOME/.local/bin/codex" --version'
    try:
        updated = subprocess.run(["ssh", "-o", "BatchMode=yes", machine, command],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=account_config.whole("codex_update_timeout_seconds"), check=False)
    except (subprocess.TimeoutExpired, OSError):
        print(f"{machine}: unreachable; run codex update there")
        return 1
    if updated.returncode == 255:
        print(f"{machine}: unreachable; run codex update there")
        return 1
    if updated.returncode:
        print(f"{machine}: update failed (exit {updated.returncode})")
        return 1
    if not updated.stdout.strip():
        print(f"{machine}: version read failed")
        return 1
    print(f"{machine}: {updated.stdout.strip().splitlines()[-1]}")
    return 0 if local_ok else 1


def _exec_codex_account(label: str, arguments: list[str], *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> int:
    """Launch Codex on a checked account, including the stored workers selection."""
    account_config = read_account_settings(account_config)
    if arguments[:1] == ["update"]:
        return _update_codex(arguments, account_config=account_config)
    selected_label = role_account("workers").label if label == "workers" else label
    target = check_codex_target(selected_label, accounts_home() / ".codex")
    environment = launch_shell_environment(Path.cwd())
    environment["CODEX_HOME"] = str(target.home)
    try:
        program = resolve_program("codex", environment=environment)
        os.execvpe(program, [program, *arguments], models_registry.agent_environment(environment))
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


def renew_login(label: str, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> LoginRenewed | LoginRenewalFailed:
    """Run a quiet probe, then keep its new usage reading. Never expose probe output."""
    account_config = read_account_settings(account_config)
    directory = claude_account_dir(label)
    timeout_seconds = account_config.whole("login_probe_timeout_seconds")
    try:
        probe = subprocess.run([str(SCRIPTS / "lib" / "py"), str(SCRIPTS / "accounts" / "accounts.py"),
            "exec", "--probe", label, "-p", "hello"], cwd=account_config.text("login_probe_directory"), stdin=subprocess.DEVNULL,
            timeout=timeout_seconds, capture_output=True)
    except subprocess.TimeoutExpired:
        return LoginRenewalFailed(f"timed out after {timeout_seconds:g} s")
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
    if (report.usage_read_outcome in (agent_accounts.UsageReadOutcome.STALE_TOKEN,
                                    agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN)
            or isinstance(report.weekly_read_skip, agent_accounts.StaleTokenSkip)):
        return LoginRenewalFailed("usage still shows no login token"
            if report.usage_read_outcome == agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN
            else "usage still shows an expired login")
    return LoginRenewed()


def exec_account(label: str, arguments: list[str], *, probe: bool = False, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> int:
    """Replace this process with the selected Claude or Codex account."""
    account_config = read_account_settings(account_config)
    label = note_file.canonical_label(label)
    if label == "workers" or _label_is(label, "codex"):
        if probe:
            raise AccountsError("accounts: --probe is only supported for Claude accounts")
        return _exec_codex_account(label, arguments, account_config=account_config)
    if label == "claude":
        prepared = PreparedAccount({}, frozenset({
            "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_REMOTE_MEMORY_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
        }))
    else:
        prepared = prepare_claude_launch(label, resume="--resume" in arguments)
    environment = launch_shell_environment(Path.cwd())
    for name in prepared.remove:
        _ = environment.pop(name, None)
    environment.update(prepared.variables)
    for name in tuple(environment):
        if name.startswith("SHOWRUNNER_"):
            _ = environment.pop(name, None)
    if probe:
        from scripts.herdr.herdr_types import NATIVE_ID_MARKS

        environment[PROBE_ENV] = "1"
        for name in NATIVE_ID_MARKS:
            _ = environment.pop(name, None)
    try:
        program = resolve_program("claude", environment=environment)
        if sys.stdin.isatty() and "RELAUNCH_SUPERVISED" not in environment:
            os.execvpe(str(SCRIPTS / "lib" / "py"), [str(SCRIPTS / "lib" / "py"), str(SCRIPTS / "lib" / "relaunch.py"),
                                      "run", "--", program, *arguments], models_registry.agent_environment(environment))
        else:
            arguments = models_registry.claude_arguments(arguments, environment)
            os.execvpe(program, [program, *arguments], models_registry.agent_environment(environment))
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
        source: ObservedStateCause | NoObservedStateCause = NO_OBSERVED_STATE_CAUSE
        label = read_role_map().workers
        if label and _label_is(label, "codex"):
            selected = agent_accounts.codex_account_home(label)
            kind: ProblemKind = "signed out" if selected.home.is_dir() else "access"
            source = ObservedStateCause(str(error), ProblemCauseKey(kind, CauseAccount(label), CauseFile(str(selected.home))))
        raise AccountsError(str(error), source=source) from error


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
    return NamedAccount(selected, claude_account_dir(selected))


def _claude_role_selection(group: ClaudeGroup, label: str) -> NamedAccount:
    directory = account_dir(label)
    state = "not set up" if not directory.is_dir() else "not logged in" if account.claude_account(directory) is None else ""
    if state:
        step = (f"run accounts.py setup {label}" if state == "not set up"
                else f"run CLAUDE_CONFIG_DIR={shlex.quote(str(directory))} claude and /login")
        inspection = account_directory_readiness(label)
        source = inspection.state_source
        if isinstance(source, NoObservedStateCause):
            kind: ProblemKind = "access" if state == "not set up" else "signed out"
            source = _observed_state_cause(NamedAccount(label, directory), kind, directory, str(inspection.problems[0]) if inspection.problems else state)
        raise AccountsError(f"accounts: {group} is on {label}, which is {state} on this machine; {step}", source=source)
    return NamedAccount(label, claude_account_dir(label))


def prepare_claude_launch(label: str, *, resume: bool) -> PreparedAccount:
    """Prepare the account or report its refusal."""
    prepared = prepared_account(label, resume=resume)
    if isinstance(prepared, AccountRefused):
        raise AccountsError(prepared.reason)
    return prepared


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
    _ = prepare_claude_launch(label, resume=resume)
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
            _ = prepare_claude_launch(target.label, resume=True)
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
    _ = prepare_claude_launch(identity.label, resume=False)
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
    candidates = [NamedAccount(claude_config_label(source_dir), source_dir)] if source_dir is not None else []
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


def checked_named_accounts(*, allow_shared_repair: bool = True) -> list[NamedAccount]:
    """Every existing named account fit for a live reading or launch."""
    return [named for named in _existing_named_accounts()
            if isinstance(_inspect_account_directory(
                named, _account_note(named.label), allow_shared_repair=allow_shared_repair,
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


def _account_note_template(tool: Literal["claude", "codex"]) -> str:
    return _codex_folder(note_file.notes_dir() / f"{tool}N.md")


def unnoted_home_line(home: Path, login: str) -> str:
    tool = "codex" if home.name.startswith(".codex") else "claude"
    return (f"~/{home.name} — no account note, not selectable: "
            f"add {_account_note_template(tool)} with login: {login}")


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
        raise AccountsError(expected.problem, source=_note_source(parsed.label, expected))
    return account_dir(parsed.label)


def claude_workers_account(label: str) -> NamedAccount:
    """Resolve a queued workers destination without changing its folder."""
    return NamedAccount(label, claude_account_dir(label))


def claude_login(directory: Path) -> str:
    """Read a quota key without making it a launch requirement."""
    identity = account_identity(directory)
    if isinstance(identity, (agent_accounts.NumberedAccount, agent_accounts.UnnotedLogin)):
        return identity.login
    account_anomaly(f"{directory}: {identity.reason}")
    return ""


def claude_account_selection(*, usage_only: bool = False) -> agent_accounts.ClaudeAccountsInUse:
    """Freeze readable accounts; usage-only selection repairs no links and reads no sessions."""
    from scripts.message import sessions

    default = account_identity(main_config_dir())
    home_state = (account.LegacyHomeLogin(account.Account("claude", default.login,
                  default.label if isinstance(default, agent_accounts.NumberedAccount) else default.login))
                  if isinstance(default, (agent_accounts.NumberedAccount, agent_accounts.UnnotedLogin))
                  else account.ConfigOnlyHome())
    named_accounts: list[agent_accounts.CheckedNamedClaudeAccount] = []
    for named in checked_named_accounts(allow_shared_repair=not usage_only):
        identity = account_identity(named.dir)
        if isinstance(identity, agent_accounts.NumberedAccount):
            named_accounts.append(agent_accounts.CheckedNamedClaudeAccount(note_file.canonical_label(named.label), named.dir, identity))
    live_logins: set[str] = set()
    live_directories: dict[Path, None] = {}
    for session in (() if usage_only else sessions.live_sessions()):
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
class ConfiguredReadingAge:
    """Read the usage age from configuration for this selection."""


_CONFIGURED_READING_AGE = ConfiguredReadingAge()


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


def pick(tool: Literal["claude", "codex"], now: datetime, *, exclude: frozenset[str] = _NO_EXCLUSIONS, keep: KeepAccount | NoKeep = _NO_KEEP, prefer_weekly_room: bool = False,
         reading_age_minutes: int | ConfiguredReadingAge = _CONFIGURED_READING_AGE, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> PickedAccount | NoAccountWithRoom:
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
    stale_minutes = (read_account_settings(account_config).whole("usage_stale_minutes")
                     if isinstance(reading_age_minutes, ConfiguredReadingAge) else reading_age_minutes)
    stale_after = timedelta(minutes=stale_minutes)
    for label, directory, login in candidates:
        excluded = (any(agent_accounts.codex_home_matches_label(directory, omitted) for omitted in exclude)
                    if tool == "codex" else label in exclude)
        if excluded or login.casefold() in seen:
            continue
        seen.add(login.casefold())
        reading = refusals.stored_reading_with_age(tool, login, now, stale_after)
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
            reason = "has room" if isinstance(reading, FreshReading) else f"has room; no reading in the last {stale_minutes} minutes"
            candidate = PickedAccount(label, directory, weekly.left if isinstance(weekly, ReadingWindow) and isinstance(reading, FreshReading) else 0,
                weekly.reset if isinstance(weekly, ReadingWindow) and isinstance(reading, FreshReading) else NoResetKnown())
            available.append(candidate)
            if not isinstance(reading, FreshReading):
                fallback.append(candidate)
        elif not isinstance(reading, FreshReading):
            reason = f"no reading in the last {stale_minutes} minutes"
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


@dataclass(frozen=True)
class LiveGroupMember:
    """A current staff member and the account of its live process."""

    session_id: str
    name: str
    group: ClaudeGroup
    config_dir: Path


def live_group_members() -> dict[ClaudeGroup, list[LiveGroupMember]]:
    """Share the board's staff membership and live process accounts."""
    return _read_local_role_sessions(GroupAccounts(), include_workers=False).members


@dataclass(frozen=True)
class InvalidProducerSettings:
    """The producer has no usable launch settings for this operation."""


_reported_producer_settings: ContextVar[set[staff.StaffId]] = ContextVar("reported_producer_settings")


@contextmanager
def settings_report_scope() -> Generator[None]:
    """Report each invalid producer once within an operation."""
    try:
        _ = _reported_producer_settings.get()
    except LookupError:
        token = _reported_producer_settings.set(set())
        try:
            yield
        finally:
            _reported_producer_settings.reset(token)
    else:
        yield


def producer_settings(member: staff.StaffRow) -> models_registry.LaunchSettings | InvalidProducerSettings:
    """Read one producer's settings and exclude it when its settings are invalid."""
    try:
        return models_registry.launch_settings("producers", member.name)
    except ValueError as error:
        reported = _reported_producer_settings.get(set())
        if member.id not in reported:
            reported.add(member.id)
            account_anomaly(f"{member.name}: {error}")
        return InvalidProducerSettings()


def eligible_producers(membership: staff.StaffSnapshot, family: str) -> tuple[staff.StaffRow, ...]:
    """Select active, unpaused, local producers of the requested family."""
    from scripts.production import staff

    machine = _session_counts_machine()
    return tuple(row for row in staff.active(membership, "producer")
                 if not isinstance(row.paused, staff.Paused)
                 and (not isinstance(row.doc, staff.LiveDoc) or row.doc.machine == machine)
                 and (not isinstance(row.worktree, staff.PresentWorktree) or row.worktree.machine == machine)
                 and isinstance(settings := producer_settings(row), models_registry.LaunchSettings)
                 and settings.kind == family)


def producer_upkeep_members() -> tuple[staff.StaffRow, ...]:
    """Select admitted local producers without a pause or an exit request."""
    from scripts.production import staff

    try:
        membership = staff.local_snapshot()
    except staff.StaffAbsent:
        return ()
    members = eligible_producers(membership, "claude") + eligible_producers(membership, "codex")
    return tuple(row for row in members if not isinstance(row.exited, staff.Exited))


@settings_report_scope()
def group_move_members(group: ClaudeGroup) -> tuple[list[LiveGroupMember], tuple[staff.StaffRow, ...]]:
    """Keep verified accounts and retain eligible members without account proof."""
    from scripts.production import staff
    from scripts.message import sessions

    live = live_group_members()[group]
    try:
        membership = staff.local_snapshot()
    except staff.StaffUnreadable:
        return live, ()
    running = {session["sessionId"] for session in sessions.running_sessions()}
    running.update(member.session_id for member in live)
    kind = "unit" if group == "units" else "showrunner"
    candidates = {row.id: row for row in membership.find(kind=kind) if row.current_session in running}
    if group == "showrunners":
        candidates.update((row.id, row) for row in eligible_producers(membership, "claude"))
    live = [member for member in live if (owner := membership.by_current_session(member.session_id)) is None
            or owner.kind != "producer" or owner.id in candidates]
    verified = {member.session_id for member in live}
    return live, tuple(row for row in candidates.values() if row.current_session not in verified)


def session_account_label(session: ListedSession | LiveGroupMember) -> str:
    return claude_config_label(session.config_dir)


def account_labels_for_logins(logins: frozenset[str]) -> frozenset[str]:
    """Exclude the numbered accounts matching these live logins."""
    labels: set[str] = set()
    for directory in (item.dir for item in checked_named_accounts()):
        identity = account_identity(directory)
        if isinstance(identity, agent_accounts.NumberedAccount) and identity.login.casefold() in logins:
            labels.add(identity.label)
    return frozenset(labels)


def placement(group: ClaudeGroup, now: datetime, *, exclude: frozenset[str] = _NO_EXCLUSIONS,
              reading_age_minutes: int | ConfiguredReadingAge = _CONFIGURED_READING_AGE, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> GroupPlacement:
    """Rank Claude accounts with room for a group using the picker's reading rules."""
    stale_minutes = (read_account_settings(account_config).whole("usage_stale_minutes")
                     if isinstance(reading_age_minutes, ConfiguredReadingAge) else reading_age_minutes)
    stale_after = timedelta(minutes=stale_minutes)
    chosen = pick("claude", now, exclude=exclude, keep=NoKeep(), reading_age_minutes=stale_minutes, account_config=account_config)
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
        reading = refusals.stored_reading_with_age("claude", identity.login, now, stale_after)
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


def placement_report(now: datetime | AccountDecisionNow = ACCOUNT_DECISION_NOW, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> str:
    account_config = read_account_settings(account_config)
    at = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    groups = group_accounts()
    room = pick("claude", at, keep=NoKeep(), account_config=account_config)
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
        chosen = placement(group, at, exclude=excluded, account_config=account_config)
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
        rows = read_json_object(accounts_config_file()).get("home_cutovers", [])
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


def _claude_login_command(directory: Path) -> str:
    """Reach this home directly without a shell alias or an inherited token."""
    return f"env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={shlex.quote(str(directory))} claude"


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


@dataclass(frozen=True)
class MoveRefused:
    reason: str


@dataclass(frozen=True)
class SessionMoveRefused:
    session_id: str
    reason: str
    returncode: int = 1


@dataclass(frozen=True)
class IdleWorkerPaneClose:
    """An idle worker can keep its conversation without a pane."""


@dataclass(frozen=True)
class SessionIdlePaneClosed:
    session_id: str


SessionMoveOutcome = stop_results.ClaudeUp | stop_results.MoveHandedOff | stop_results.ResumePending | SessionMoveRefused | SessionIdlePaneClosed


class WorkerResumePreview(Protocol):
    """The observed source account for a worker that can resume on the target."""

    @property
    def source(self) -> str: ...


class PaneMoves(Protocol):
    def move_worker(self, row: staff.StaffRow, record: staff.StaffSnapshot, target: str,
                    why: str, run: Path) -> SessionMoveOutcome: ...

    def run_for_worker(self, row: staff.StaffRow, record: staff.StaffSnapshot) -> Path: ...

    def preview_worker(self, row: staff.StaffRow, record: staff.StaffSnapshot, target: str,
                       why: str, run: Path) -> WorkerResumePreview | IdleWorkerPaneClose | SessionMoveRefused | stop_results.ClaudeUp: ...


class AccountWorkerActivity(Protocol):
    @property
    def row(self) -> staff.StaffRow: ...


class AccountPaneReader(Protocol):
    OpenWorker: type[AccountWorkerActivity]

    def observe_parents(self, parents: Sequence[staff.StaffRow], record: staff.StaffSnapshot
                        ) -> dict[staff.StaffId, list[AccountWorkerActivity]]: ...

    def worker_state(self, observed: AccountWorkerActivity) -> Literal["idle", "busy", "unknown"]: ...


class SessionBatchMove(Protocol):
    def __call__(self, sessions: list[str], target: str, *, why: str, cause: moves.MoveCause,
                 source_accounts: Mapping[str, str]) -> list[stop_results.MoveResult]: ...


def move_sessions(ids: tuple[str, ...], target: str, *, why: str, cause: moves.MoveCause = "by hand", live: Sequence[ListedSession] | None = None, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> tuple[SessionMoveOutcome, ...]:
    """Stop and resume each selected local owner through the shared move API."""

    account_config = read_account_settings(account_config)
    if TYPE_CHECKING:
        from scripts.agents import inventory
    else:
        inventory = import_module("scripts.agents.inventory")

    checked = check_move_target(target, datetime.now(timezone.utc), account_config=account_config)
    if isinstance(checked, MoveRefused):
        return tuple(SessionMoveRefused(identity, checked.reason) for identity in ids)
    live = inventory.listing() if live is None else live
    selected = list(dict.fromkeys(ids))
    refused: dict[str, SessionMoveRefused] = {}
    eligible: list[str] = []
    for identity in selected:
        matches = [item for item in live if item.session_id == identity]
        if len(matches) > 1:
            refused[identity] = SessionMoveRefused(identity, "session has several live processes")
        else:
            eligible.append(identity)
    outcomes: dict[str, SessionMoveOutcome] = dict(refused)
    batch = cast(SessionBatchMove, import_module("scripts.agents.stop_resume").move_sessions)
    sources = {item.session_id: claude_config_label(item.config_dir) for item in live if item.session_id in eligible}
    results = batch(eligible, checked.destination.label, why=why, cause=cause, source_accounts=sources)
    for identity, result in zip(eligible, results):
        if isinstance(result, stop_results.SessionNotFound):
            account_anomaly(f"{identity}: no saved session was found")
        elif isinstance(result, (stop_results.ClaudeUp, stop_results.MoveHandedOff, stop_results.ResumePending)):
            outcomes[identity] = result
        else:
            outcomes[identity] = SessionMoveRefused(identity, result.reason)
    return tuple(outcomes[identity] for identity in selected if identity in outcomes)


def move_result_line(result: SessionMoveOutcome) -> str:
    if isinstance(result, SessionIdlePaneClosed):
        return f"{result.session_id}: idle pane closed; the next assign resumes its conversation"
    if isinstance(result, SessionMoveRefused):
        return f"{result.session_id}: not moved: {result.reason}"
    if isinstance(result, stop_results.ResumePending):
        return f"{result.session_id}: stopped, resume pending on {result.account}: {next(iter(result.reason.splitlines()), 'unknown error')}"
    if isinstance(result, stop_results.MoveHandedOff):
        return f"{result.session_id}: moving now"
    return stop_results.claude_up_line(result)


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
    outcomes: tuple[SessionMoveOutcome, ...] = field(default=(), kw_only=True)


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
    outcomes: tuple[SessionMoveOutcome, ...] = field(default=(), kw_only=True)


@dataclass(frozen=True)
class FollowWorkersRole:
    """Move Codex workers to the current workers role."""


@dataclass(frozen=True)
class FixedClaudeWorkersTarget:
    """Move Claude workers to this account without reading the workers role."""

    label: str


def move_workers(target: str, *, dry_run: bool = False, why: str = "by hand",
                 follow_role: bool = False,
                 account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS
                 ) -> GroupMoveStarted | GroupMoveStartFailed | GroupMoveDryRun | MoveRefused:
    """Follow the Codex workers role or move Claude workers to the requested account."""
    target = note_file.canonical_label(target)
    if not follow_role or dry_run:
        if _label_is(target, "codex"):
            selected = _current_workers_target()
            if target != selected:
                return MoveRefused(f"workers role is {selected}; run accounts.py swap workers {target} to select it")
            _ = _codex_move_target(target)
        else:
            checked = check_move_target(target, datetime.now(timezone.utc), repair=False, account_config=account_config)
            if isinstance(checked, MoveRefused):
                return checked
    if not dry_run:
        destination = FollowWorkersRole() if _label_is(target, "codex") else FixedClaudeWorkersTarget(target)
        return _run_worker_moves(destination, why)
    return _preview_worker_moves(target, why)


def _workers_move_rows(record: staff.StaffSnapshot, family: str) -> tuple[staff.StaffRow, ...]:
    """Include Codex producers in the workers account selection."""
    from scripts.production import session_staff

    producers: set[staff.StaffId] = {row.id for row in eligible_producers(record, family)} if family == "codex" else set()
    return tuple(row for row in record.rows
                 if (row.kind == "worker" and (not row.current_session or
                     session_staff.worker_family(record, row.id) == family)) or
                    row.id in producers)


@settings_report_scope()
def _preview_worker_moves(target: str, why: str) -> GroupMoveDryRun:
    """Preview workers after the caller checks the destination."""
    from scripts.production import staff
    pane_moves = cast(PaneMoves, cast(object, import_module("scripts.accounts.pane_moves")))
    record = staff.local_snapshot()
    family = "codex" if _label_is(target, "codex") else "claude"
    rows = _workers_move_rows(record, family)
    identities = tuple(str(row.id) for row in rows)

    lines: list[str] = []
    nothing = 0
    for row in rows:
        if row.kind == "producer":
            lines.append(f"{row.name} ({row.id}): would stop and resume the same conversation on {target}")
            continue
        try:
            run = pane_moves.run_for_worker(row, record)
            decision = pane_moves.preview_worker(row, record, target, why, run)
        except (OSError, ValueError, AccountsError, staff.StaffRefused, herdr_types.HerdrFailure) as error:
            decision = SessionMoveRefused(row.id, str(error).replace("\n", " "))
        if isinstance(decision, stop_results.ClaudeUp) or isinstance(decision, SessionMoveRefused) and decision.returncode == 0:
            nothing += 1
            continue
        if isinstance(decision, IdleWorkerPaneClose):
            preview = f"would close its idle pane; the next assign resumes it on {target}"
        else:
            preview = (f"not moved: {decision.reason}" if isinstance(decision, SessionMoveRefused) else
                       f"would stop and resume the same conversation on {target} in its worker pane")
        lines.append(f"{row.name} ({row.id}): {preview}")
    if nothing:
        lines.append(f"{nothing} workers: nothing to move")
    return GroupMoveDryRun(identities, tuple(lines))


def _move_worker_row(pane_moves: PaneMoves, row: staff.StaffRow, record: staff.StaffSnapshot,
                     target: str, why: str) -> SessionMoveOutcome:
    from scripts.production import staff

    try:
        if row.kind == "producer":
            batch = cast(SessionBatchMove, import_module("scripts.agents.stop_resume").move_sessions)
            result = batch([str(row.current_session)], target, why=why, cause="by hand", source_accounts={})[0]
            if isinstance(result, stop_results.SessionNotFound):
                return SessionMoveRefused(row.current_session, "no saved session was found")
            if isinstance(result, (stop_results.ClaudeUp, stop_results.MoveHandedOff, stop_results.ResumePending)):
                return result
            return SessionMoveRefused(row.current_session, result.reason)
        run = pane_moves.run_for_worker(row, record)
        return pane_moves.move_worker(row, record, target, why, run)
    except (OSError, ValueError, AccountsError, staff.StaffRefused, herdr_types.HerdrFailure) as error:
        return SessionMoveRefused(row.id, str(error).replace("\n", " "))


def _current_workers_target() -> str:
    """Read the authority without reusing the command's earlier observation."""
    return Context().run(read_role_map).workers


def _resolve_worker_move_target(destination: FollowWorkersRole | FixedClaudeWorkersTarget) -> str:
    return _current_workers_target() if isinstance(destination, FollowWorkersRole) else destination.label


@settings_report_scope()
def _run_worker_moves(destination: FollowWorkersRole | FixedClaudeWorkersTarget,
                      why: str) -> GroupMoveStarted | GroupMoveStartFailed:
    from scripts.production import staff

    pane_moves = cast(PaneMoves, cast(object, import_module("scripts.accounts.pane_moves")))
    results: dict[staff.StaffId, SessionMoveOutcome] = {}
    attempted: dict[staff.StaffId, str] = {}
    family = "codex" if isinstance(destination, FollowWorkersRole) else "claude"
    with _workers_move_lock("run"):
        if isinstance(destination, FollowWorkersRole):
            with _workers_move_lock("schedule"):
                if isinstance(_read_pending_worker_moves(), NoPendingWorkerMoves):
                    _queue_worker_moves(_resolve_worker_move_target(destination))
        while True:
            record = staff.local_snapshot()
            for row in _workers_move_rows(record, family):
                target = _resolve_worker_move_target(destination)
                if attempted.get(row.id) == target:
                    continue
                results[row.id] = _move_worker_row(pane_moves, row, record, target, why)
                attempted[row.id] = target
            with _workers_move_lock("schedule") if isinstance(destination, FollowWorkersRole) else nullcontext():
                target = _resolve_worker_move_target(destination)
                latest = staff.local_snapshot()
                if any(attempted.get(row.id) != target for row in _workers_move_rows(latest, family)):
                    continue
                if isinstance(destination, FollowWorkersRole):
                    pending = _read_pending_worker_moves()
                    if isinstance(pending, PendingWorkerMoves):
                        _complete_worker_move_requests(pending.requests, target)
                        _workers_move_file().unlink()
                break
    outcomes = tuple(results.values())
    stdout, stderr, code = _move_output(outcomes, ())
    outcome = GroupMoveStartFailed if code else GroupMoveStarted
    return outcome(tuple(str(identity) for identity in results), target, stdout, stderr, code, outcomes=outcomes)



def _move_target(target: str, *, repair: bool = True) -> NamedAccount | MoveRefused:
    target = note_file.canonical_label(target)
    try:
        refuse_default(target)
        prepared = (prepared_account(target, resume=True) if repair
                    else prepared_account_preview(target, resume=True))
        if isinstance(prepared, AccountRefused):
            return MoveRefused(prepared.reason.partition("\n")[0])
        destination = NamedAccount(target, claude_account_dir(target))
    except AccountsError as error:
        return MoveRefused(str(error).partition("\n")[0])
    return destination


@dataclass(frozen=True)
class MoveTargetReady:
    destination: NamedAccount
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class MoveDryRun:
    session_id: str
    lines: tuple[str, ...]


def check_move_target(target: str, now: datetime, *, repair: bool = True, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> MoveTargetReady | MoveRefused:
    """Check the destination without changing a group or starting a shutdown."""
    account_config = read_account_settings(account_config)
    destination = _move_target(target, repair=repair)
    if isinstance(destination, MoveRefused):
        return destination
    reading = refusals.stored_reading("claude", claude_login(destination.dir), now)
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
                    chosen = pick("claude", now, exclude=frozenset({destination.label}), keep=NoKeep(), account_config=account_config)
                    room = f"{chosen.label} has room" if isinstance(chosen, PickedAccount) else no_room_line("claude", chosen)
                    return MoveRefused(f"accounts: {destination.label} is out of usage ({name} {window.left:g}% left, resets {reset}); {room}")
        return MoveTargetReady(destination, ())
    windows = ((reading.weekly, reading.five_hour) if isinstance(reading, StaleReading) else ())
    times = [window.at for window in windows if isinstance(window, ReadingWindow)]
    age = f"last {max(0, int((now - max(times)).total_seconds() // 60))} minutes ago" if times else "last reading unknown"
    return MoveTargetReady(destination, (f"no fresh usage reading for {destination.label} ({age})",))


def move_preview_line(member: ListedSession | LiveGroupMember, target: str) -> str:
    return f"{member.name}: {member.session_id} · stop, then resume on {target} (from {session_account_label(member)})"


def _move_output(results: tuple[SessionMoveOutcome, ...],
                 notices: tuple[str, ...]) -> tuple[str, str, int]:
    stdout = "" if results else "".join(line + "\n" for line in notices)
    stderr = ""
    for result in results:
        line = move_result_line(result) + ("; " + "; ".join(notices) if notices else "") + "\n"
        if isinstance(result, (SessionMoveRefused, stop_results.ResumePending)):
            stderr += line
        else:
            stdout += line
    return stdout, stderr, max((item.returncode if isinstance(item, SessionMoveRefused) else
                               int(isinstance(item, stop_results.ResumePending)) for item in results), default=0)


@role_map.role_map_command
def move_group(group: ClaudeGroup, target: str, *, dry_run: bool = False, why: str = "", alerts: MoveAlerts = "user", account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> GroupMoveStarted | GroupMoveStartFailed | GroupMoveDryRun | MoveRefused:
    """Select the group after target checks, then stop and resume each owner."""
    from scripts.production import staff

    account_config = read_account_settings(account_config)
    del alerts
    target = note_file.canonical_label(target)
    now = datetime.now(timezone.utc)
    checked = check_move_target(target, now, repair=not dry_run, account_config=account_config)
    if isinstance(checked, MoveRefused):
        return checked
    members, stopped = group_move_members(group)
    selected = [session for session in members if not session_on_account(session, target)]
    ids = (*tuple(session.session_id for session in selected), *(row.current_session for row in stopped))
    reason = why or f"{group} moved by hand"
    notices = (f"{group}: no live session moves",) if not ids else ()
    notices += tuple(f"{row.name}: saved account is unknown; resume on {target}"
                     for row in stopped if not isinstance(row.home, staff.SessionHome))
    if group == "showrunners":
        if TYPE_CHECKING:
            from scripts.agents import inventory
        else:
            inventory = import_module("scripts.agents.inventory")
        live_ids = {member.session_id for member in members}
        notices += tuple(
            f"{runner.name}: no session runs under the id its update timer names; "
            + f"in any Claude session on {socket.gethostname().split('.', 1)[0]}, inside that showrunner's own session: ~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py register --production {shlex.quote(runner.name)}"
            for runner in inventory.showrunner_registrations().entries if runner.session_id not in live_ids)
    if dry_run:
        return GroupMoveDryRun(ids, (*checked.notices, f"{group}: would select {target}",
            *(move_preview_line(item, target) for item in selected),
            *(f"{row.name}: {row.current_session} · resume on {target} (from {claude_config_label(row.home.path)})"
              for row in stopped if isinstance(row.home, staff.SessionHome)), *notices))
    _ = set_group_account(group, target, why=reason)
    results = move_sessions(ids, target, why=reason, account_config=account_config) if ids else ()
    stdout, stderr, code = _move_output(results, (*checked.notices, *notices))
    outcome = GroupMoveStartFailed if code else GroupMoveStarted
    return outcome(ids, target, stdout, stderr, code)


def session_on_account(session: ListedSession | LiveGroupMember, label: str) -> bool:
    if _label_is(label, "codex"):
        return agent_accounts.codex_home_matches_label(session.config_dir, label)
    return _label_is(label, "claude") and session.config_dir == claude_account_dir(label)


@overload
def move(session: str, target: str, *, dry_run: Literal[False] = False, why: str = "", alerts: MoveAlerts = "user", account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> MoveStarted | MoveStartFailed | MoveRefused: ...


@overload
def move(session: str, target: str, *, dry_run: Literal[True], why: str = "", alerts: MoveAlerts = "user", account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> MoveDryRun | MoveRefused: ...


@overload
def move(session: str, target: str, *, dry_run: bool, why: str = "", alerts: MoveAlerts = "user", account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> MoveStarted | MoveStartFailed | MoveDryRun | MoveRefused: ...


def move(session: str, target: str, *, dry_run: bool = False, why: str = "", alerts: MoveAlerts = "user", account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> MoveStarted | MoveStartFailed | MoveDryRun | MoveRefused:
    """Skip an owner on the target; check quota before moving another owner."""
    account_config = read_account_settings(account_config)
    del alerts
    target = note_file.canonical_label(target)
    if TYPE_CHECKING:
        from scripts.agents import inventory
    else:
        inventory = import_module("scripts.agents.inventory")
    try:
        refuse_default(target)
        try:
            live = inventory.listing()
        except (inventory.InvalidInventory, OSError, ValueError):
            checked = check_move_target(target, datetime.now(timezone.utc), repair=not dry_run, account_config=account_config)
            if isinstance(checked, MoveRefused):
                return checked
            raise
        matches = [item for item in live if item.session_id == session]
        if not matches:
            matches = [item for item in live if item.name == session]
        if len(matches) == 1 and session_account_label(matches[0]) == target:
            owner = matches[0]
            line = f"{owner.name}: already on {target}"
            return MoveDryRun(owner.session_id, (line,)) if dry_run else MoveStarted(owner.session_id, target, line + "\n", "", 0)
        checked = check_move_target(target, datetime.now(timezone.utc), repair=not dry_run, account_config=account_config)
        if isinstance(checked, MoveRefused):
            return checked
        if not matches:
            if dry_run:
                return MoveDryRun(session, (f"{session}: would resume on {target} if its saved tab is local",))
            results = move_sessions((session,), target, why=why or "finish account move", live=live, account_config=account_config)
            stdout, stderr, code = _move_output(results, ())
            outcome = MoveStartFailed if code else MoveStarted
            return outcome(session, target, stdout, stderr, code)
        if len(matches) != 1:
            return MoveRefused(f"{session} names multiple live sessions: " + ", ".join(item.session_id for item in matches))
        owner = matches[0]
        group = session_group(owner)
        return_notice: tuple[str, ...] = ()
        if group != "ungrouped":
            selected = group_label(group_accounts(), group)
            if isinstance(selected, str) and selected != target:
                return_notice = (f"{owner.name} returns to {selected} at its next resume; "
                                 + f"to keep {target}, run: accounts.py move {group} {target}",)
        reason = why or f"{session_account_label(owner)} is out of usage"
        if dry_run:
            return MoveDryRun(owner.session_id, (*checked.notices, move_preview_line(owner, target), *return_notice))
        results = move_sessions((owner.session_id,), target, why=reason, live=live, account_config=account_config)
        stdout, stderr, code = _move_output(results, checked.notices)
        stdout += "".join(line + "\n" for line in return_notice)
        outcome = MoveStartFailed if code else MoveStarted
        return outcome(owner.session_id, target, stdout, stderr, code)
    except (AccountsError, inventory.InvalidInventory, OSError, ValueError) as error:
        return MoveRefused(str(error))


def _codex_move_target(label: str) -> SelectableCodexAccount:
    """Validate the account without preparing a worker restart."""
    label = note_file.canonical_label(label)
    refuse_default(label)
    home = _codex_account_home(label)
    expected = _account_note(label)
    if not isinstance(expected, ExpectedAccountLogin):
        raise AccountsError(expected.problem)
    return SelectableCodexAccount(home, _codex_home_login(home), label)


def check_codex_target(label: str, workers_home: Path, *, repair: bool = True) -> SelectableCodexAccount:
    del workers_home
    target = _codex_move_target(label)
    problems = (_repair_shared_links if repair else _shared_repair_problems)(
        target.home, (accounts_home() / ".codex").absolute(), CODEX_SHARED, CODEX_ALWAYS_LINKED,
    )
    for problem in problems:
        account_anomaly(problem)
    return target


def codex_home_label(home: Path) -> str:
    """The account's note label, or its folder when the login has no note."""
    return agent_accounts.codex_home_role_label(home)


WorkerMoves = Literal["wait", "detach"]


@dataclass(frozen=True)
class PendingWorkerMoves:
    """Accepted group requests await the end of a detached workers move."""

    requests: tuple[str, ...]


@dataclass(frozen=True)
class NoPendingWorkerMoves:
    """No detached workers move needs a retry."""


@dataclass(frozen=True)
class KeepWorkersRole:
    """Restart pending moves without changing the selected account."""


KEEP_WORKERS_ROLE = KeepWorkersRole()


class WorkerMovesDocument(TypedDict):
    requests: list[str]


def _workers_move_file() -> Path:
    return moves.store_file().with_name("workers_move.json")


@contextmanager
def _workers_move_lock(purpose: Literal["run", "schedule"]) -> Generator[None]:
    path = _workers_move_file().with_suffix(f".{purpose}.lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with file_lock.exclusive(path):
        yield


def _pending_workers_requests(
    journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL,
) -> dict[str, moves.RequestedMove]:
    pending: dict[str, moves.RequestedMove] = {}
    entries = moves.read_moves() if isinstance(journal, moves.ReadCurrentMoveJournal) else journal
    for entry in entries:
        if entry["subject"] != "workers":
            continue
        if entry["kind"] == "requested":
            pending[entry["move_id"]] = entry
        elif entry["move_id"] in pending:
            del pending[entry["move_id"]]
    return pending


def _read_pending_worker_moves() -> PendingWorkerMoves | NoPendingWorkerMoves:
    try:
        raw = cast(object, json.loads(_workers_move_file().read_text(encoding="utf-8")))
    except FileNotFoundError:
        return NoPendingWorkerMoves()
    if not isinstance(raw, dict):
        raise AccountsError("accounts: invalid pending workers move")
    requests = cast(dict[str, object], raw).get("requests")
    if not isinstance(requests, list) or not all(isinstance(value, str) for value in cast(list[object], requests)):
        raise AccountsError("accounts: invalid pending workers move requests")
    return PendingWorkerMoves(tuple(cast(list[str], requests)))


def _queue_worker_moves(
    label: str, journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL,
) -> None:
    requests = tuple(_pending_workers_requests(journal).values())
    saved = _read_pending_worker_moves()
    # Keep saved requests even when this pass has an older journal snapshot.
    accepted = list(saved.requests) if isinstance(saved, PendingWorkerMoves) else []
    boundary = next((index + 1 for index in range(len(requests) - 1, -1, -1)
                     if requests[index]["to"] == label), len(requests))
    accepted.extend(request["move_id"] for request in requests[:boundary] if request["move_id"] not in accepted)
    document = WorkerMovesDocument(requests=accepted)
    atomic_file.write_text_atomic(_workers_move_file(), json.dumps(document) + "\n", new_file_mode=0o600)


def _complete_worker_move_requests(requests: tuple[str, ...], target: str) -> None:
    pending = _pending_workers_requests()
    home = str(_codex_account_home(target) if _label_is(target, "codex") else claude_account_dir(target))
    for identity in requests:
        request = pending.get(identity)
        if request is None:
            continue
        common = moves.MoveCommon(at=datetime.now(timezone.utc).isoformat(), tool=request["tool"],
                                  subject="workers", move_id=identity)
        if request["to"] == target:
            moves.append_move(moves.ConfirmedMove(**common, kind="confirmed", home=home))
        else:
            moves.append_move(moves.FailedMove(**common, kind="failed", reason=f"superseded by {target}"))


def _launch_worker_moves(label: str, why: str) -> None:
    command = [str(SCRIPTS / "lib/py"), str(SCRIPTS / "accounts/accounts.py"), "move", "workers", label,
               "--follow-role", "--why", why]
    try:
        if shutil.which("systemd-run") is None:
            _ = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            return
        unit = "accounts-workers-move"
        if subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], check=False).returncode == 0:
            return
        started = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", *command],
                                 capture_output=True, text=True, check=False)
    except OSError as error:
        raise WorkerMovesStartFailed(f"accounts: could not start the workers move: {error}") from error
    if started.returncode:
        raise WorkerMovesStartFailed(f"accounts: could not start the workers move: {started.stderr.strip()}")


def _start_worker_moves(
    label: str, why: str, *, change: RoleMapChange | KeepWorkersRole = KEEP_WORKERS_ROLE,
    journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL,
) -> None:
    """Write the role and pending work under the final-check lock, then start one detached move."""
    with _workers_move_lock("schedule"):
        if isinstance(change, RoleMapChange):
            write_role_map(change)
        _queue_worker_moves(label, journal)
        _launch_worker_moves(label, why)


def resume_worker_moves(
    journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL,
) -> None:
    """Restart an unfinished handoff without replacing its role selection."""
    with _workers_move_lock("schedule"):
        pending = _read_pending_worker_moves()
        entries = moves.read_moves() if isinstance(journal, moves.ReadCurrentMoveJournal) else journal
        requests = _pending_workers_requests(entries)
        if isinstance(pending, NoPendingWorkerMoves) and not requests:
            return
        target = _current_workers_target()
        if isinstance(pending, NoPendingWorkerMoves):
            if not any(request["to"] == target for request in requests.values()):
                return
            _queue_worker_moves(target, entries)
        why = next((request["why"] for request in reversed(tuple(requests.values())) if request["to"] == target),
                   "retry workers move")
        _launch_worker_moves(target, why)


def _swap_workers(label: str, *, dry_run: bool, why: str = "by hand",
                  cause: Literal["by hand", "refusal", "auto", "credits off"] = "by hand",
                  included_only: bool = False, worker_moves: WorkerMoves = "wait",
                  worker_move_journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL,
                  ) -> WorkersSwapOutcome:
    """Check the live allowance before changing the workers account."""
    refuse_default(label)
    try:
        workers_home = _workers_home()
    except AccountsError:
        # An explicit swap can configure an unset/unavailable local workers role;
        # reading the authoritative map must still succeed before it proceeds.
        _ = read_role_map()
        workers_home = accounts_home() / ".codex"
    target = check_codex_target(label, workers_home, repair=not dry_run)
    account_label, target_home = target.label, target.home
    report = _codex_live(target_home)
    if report.problem:
        account_anomaly(f"{account_label} ({_codex_folder(target_home)}): {report.problem}")
    credits = agent_accounts.CreditsOff() if included_only else agent_accounts.codex_credits_setting(account_label)
    readiness = agent_accounts.codex_can_work(report, datetime.now(timezone.utc), credits)
    if isinstance(readiness, agent_accounts.CodexWorkExhausted):
        if included_only:
            raise AccountsError(f"accounts: {account_label} ({_codex_folder(target_home)}) has no included usage left; choose another account or wait for reset")
        reason = "and set not to use credits" if isinstance(credits, agent_accounts.CreditsOff) else "with no credits"
        raise AccountsError(f"accounts: {account_label} ({_codex_folder(target_home)}) is used up {reason}; choose another account or wait for reset")
    if isinstance(readiness, agent_accounts.CodexWorkUnknown):
        if not report.problem:
            account_anomaly(f"{account_label} ({_codex_folder(target_home)}): {readiness.problem}")
    if dry_run:
        preview = _preview_worker_moves(account_label, why)
        return WorkersSwapDryRun(preview.lines)
    try:
        if worker_moves == "detach":
            change = RoleMapChange("workers", account_label, why, datetime.now(timezone.utc).isoformat(), cause=cause)
            _start_worker_moves(account_label, why, change=change, journal=worker_move_journal)
            return WorkersSwapStarted(())
        _ = _write_role("workers", account_label if _label_is(account_label, "codex") else label, why=why, cause=cause)
    except OSError as error:
        raise AccountsError(f"accounts: could not move the workers role: {error}") from error
    moved = move_workers(account_label, why=why, follow_role=True)
    if isinstance(moved, MoveRefused):
        raise AccountsError(moved.reason)
    assert not isinstance(moved, GroupMoveDryRun)
    return WorkersSwapStarted(moved.outcomes)


def swap(role: str, label: str, *, dry_run: bool = False, why: str = "by hand",
         cause: Literal["by hand", "refusal", "auto", "credits off"] = "by hand",
         included_only: bool = False, worker_moves: WorkerMoves = "wait",
         worker_move_journal: Sequence[moves.MoveEntry] | moves.ReadCurrentMoveJournal = moves.READ_CURRENT_MOVE_JOURNAL,
         ) -> SwapOutcome | WorkersSwapOutcome:
    """Move workers; included_only requires room without paid credits at the live check.

    "detach" writes the role, then moves the workers in their own process and returns at once.
    """
    _validate_role(role)
    if role == "workers":
        return _swap_workers(label, dry_run=dry_run, why=why, cause=cause, included_only=included_only,
                             worker_moves=worker_moves, worker_move_journal=worker_move_journal)
    raise AccountsError(UNIT_ROLE_GONE)


def _is_known_private(name: str) -> bool:
    return (
        name in KNOWN_PRIVATE_NAMES
        or name.startswith("daemon-auth-")
        or name.startswith(".oauth_refresh.lock")
        or name.startswith(".claude.json.tmp.")
        or name.startswith(".claude.json.mcp-backup-")
        or name == ".mcp-repair.lock"
    )


def sweep_claude_temp_files(now: datetime, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> None:
    """Remove old regular temporary files without following links."""
    account_config = read_account_settings(account_config)
    home = accounts_home()
    folders = [home]
    stale_before = (now - timedelta(days=account_config.whole("temporary_file_age_days"))).timestamp()
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


@dataclass(frozen=True)
class UnreadableMcpConfiguration:
    """No comparison can be made from this configuration."""

    path: Path
    reason: str


@dataclass(frozen=True)
class MissingMcpFile:
    """Claude has not created a configuration file yet."""


@dataclass(frozen=True)
class ExistingMcpFile:
    """The original text and permissions to preserve during a repair."""

    text: str
    mode: int


@dataclass(frozen=True)
class ReadMcpConfiguration:
    names: frozenset[str]
    file: ExistingMcpFile | MissingMcpFile
    global_names: frozenset[str] = frozenset()
    projects: tuple[ProjectMcpServers, ...] = ()
    global_definitions: dict[str, object] = field(default_factory=dict)
    configuration: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectMcpServers:
    """The effective servers for sessions launched in this project."""

    path: str
    names: frozenset[str]
    definitions: dict[str, object] = field(default_factory=dict)


McpDirection = Literal["off", "on"]


@dataclass(frozen=True)
class McpServerDifference:
    """The scopes where one server changes in one direction."""

    server: str
    direction: McpDirection
    projects: tuple[str, ...]


@dataclass(frozen=True)
class McpServerDifferences:
    entries: tuple[McpServerDifference, ...] = ()


@dataclass(frozen=True)
class McpConfigurationInspection:
    """The comparison and faults from inspecting both configurations safely."""

    differences: McpServerDifferences
    account_is_safe_to_inspect: bool
    baseline_problems: tuple[UnreadableMcpConfiguration, ...]
    account_problems: tuple[UnreadableMcpConfiguration, ...]
    baseline: ReadMcpConfiguration | UnreadableMcpConfiguration
    account: ReadMcpConfiguration | UnreadableMcpConfiguration


def _mcp_server_names(config_file: Path, project: Path) -> ReadMcpConfiguration | UnreadableMcpConfiguration:
    try:
        with config_file.open(encoding="utf-8") as stream:
            source = ExistingMcpFile(stream.read(), stat.S_IMODE(os.fstat(stream.fileno()).st_mode))
        configuration = read_json_object(io.StringIO(source.text))
    except FileNotFoundError:
        # Claude can start without a configuration file.
        return ReadMcpConfiguration(frozenset(), MissingMcpFile())
    except (OSError, ValueError, TypeError) as error:
        return UnreadableMcpConfiguration(config_file, str(error))
    return _mcp_configuration(configuration, project, source)


def _mcp_configuration(configuration: dict[str, object], project: Path,
                       source: ExistingMcpFile | MissingMcpFile) -> ReadMcpConfiguration:
    """Resolve effective server definitions once for readers and repairs."""
    def definitions(servers: object) -> dict[str, object]:
        return {name: value for name, value in cast(dict[object, object], servers).items()
                if isinstance(name, str)} if isinstance(servers, dict) else {}
    global_definitions = definitions(configuration.get("mcpServers", {}))
    global_names = frozenset(global_definitions)
    raw_projects = configuration.get("projects", {})
    projects: list[ProjectMcpServers] = []
    if isinstance(raw_projects, dict):
        for path, local in cast(dict[object, object], raw_projects).items():
            if not isinstance(path, str) or not isinstance(local, dict):
                continue
            values = cast(dict[str, object], local)
            local_definitions = definitions(values.get("mcpServers", {}))
            active = set(global_names) | local_definitions.keys()
            disabled = values.get("disabledMcpServers", [])
            if isinstance(disabled, list):
                active.difference_update(name for name in cast(list[object], disabled) if isinstance(name, str))
            projects.append(ProjectMcpServers(path, frozenset(active), local_definitions))
    home_names = next((scope.names for scope in projects if scope.path == str(project.resolve())), global_names)
    return ReadMcpConfiguration(home_names, source, global_names, tuple(projects), global_definitions, configuration)


def _mcp_differences(baseline: ReadMcpConfiguration | UnreadableMcpConfiguration,
                     current: ReadMcpConfiguration | UnreadableMcpConfiguration, directory: Path) -> McpServerDifferences:
    if not isinstance(baseline, ReadMcpConfiguration) or not isinstance(current, ReadMcpConfiguration):
        return McpServerDifferences()
    baseline_projects = {scope.path: scope.names for scope in baseline.projects}
    current_projects = {scope.path: scope.names for scope in current.projects}
    homes = {str(main_config_dir().resolve()), str(directory.resolve())}
    comparisons = [("~", baseline.names, current.names), *((path, baseline_projects.get(path, baseline.global_names),
        current_projects.get(path, current.global_names)) for path in sorted(baseline_projects.keys() | current_projects.keys())
        if path not in homes)]
    projects: dict[tuple[str, McpDirection], list[str]] = {}
    for path, before, after in comparisons:
        for server in before - after:
            projects.setdefault((server, "off"), []).append(path)
        for server in after - before:
            projects.setdefault((server, "on"), []).append(path)
    return McpServerDifferences(tuple(McpServerDifference(server, direction, tuple(paths))
        for (server, direction), paths in sorted(projects.items())))


def _inspect_mcp_configuration(directory: Path) -> McpConfigurationInspection:
    baseline = _mcp_server_names(agent_accounts.claude_config_file(main_config_dir()), main_config_dir())
    config_file = directory / ".claude.json"
    account_is_safe_to_inspect = True
    current = _mcp_server_names(config_file, main_config_dir())
    return McpConfigurationInspection(
        _mcp_differences(baseline, current, directory), account_is_safe_to_inspect,
        (baseline,) if isinstance(baseline, UnreadableMcpConfiguration) else (),
        (current,) if account_is_safe_to_inspect and isinstance(current, UnreadableMcpConfiguration) else (),
        baseline, current,
    )


def mcp_projects(label: str, server: str, direction: McpDirection) -> tuple[str, ...]:
    """Read the differing scopes without changing either configuration."""
    directory = account_dir(label)
    inspection = _inspect_mcp_configuration(directory)
    for configuration in (*inspection.baseline_problems, *inspection.account_problems):
        raise AccountsError(f"accounts: cannot read MCP configuration {configuration.path}: {configuration.reason}")
    return next((entry.projects for entry in inspection.differences.entries
                 if entry.server == server and entry.direction == direction), ())


def mcp_repair(label: str, server: str) -> None:
    """Match one server's effective project settings to the shared baseline."""
    directory = account_dir(label)
    config_file = directory / ".claude.json"
    with (directory / ".mcp-repair.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        inspection = _inspect_mcp_configuration(directory)
        for fault in (*inspection.baseline_problems, *inspection.account_problems):
            raise AccountsError(f"accounts: cannot read MCP configuration {fault.path}: {fault.reason}")
        baseline, current = inspection.baseline, inspection.account
        if not isinstance(baseline, ReadMcpConfiguration) or not isinstance(current, ReadMcpConfiguration):
            return
        source = current.file
        differences = [entry for entry in inspection.differences.entries if entry.server == server]
        if not differences:
            return
        configuration = current.configuration
        raw_globals = configuration.get("mcpServers", {})
        if not isinstance(raw_globals, dict):
            raise AccountsError("accounts: MCP servers must be an object; no configuration changed")
        global_servers = dict(cast(dict[str, object], raw_globals))
        if server in baseline.global_definitions:
            global_servers[server] = baseline.global_definitions[server]
        else:
            _ = global_servers.pop(server, None)
        configuration["mcpServers"] = global_servers
        current = _mcp_configuration(configuration, main_config_dir(), source)
        differences = [entry for entry in _mcp_differences(baseline, current, directory).entries
                       if entry.server == server]
        raw_projects = configuration.setdefault("projects", {})
        if not isinstance(raw_projects, dict):
            raise AccountsError("accounts: MCP projects must be an object; no configuration changed")
        projects = cast(dict[str, object], raw_projects)
        for entry in differences:
            for scope in entry.projects:
                project = str(main_config_dir().resolve()) if scope == "~" else scope
                raw_local = projects.setdefault(project, {})
                if not isinstance(raw_local, dict):
                    raise AccountsError("accounts: MCP project must be an object; no configuration changed")
                local = cast(dict[str, object], raw_local)
                raw_disabled = local.get("disabledMcpServers", [])
                raw_servers = local.get("mcpServers", {})
                if not isinstance(raw_disabled, list) or not isinstance(raw_servers, dict):
                    raise AccountsError("accounts: invalid MCP project settings; no configuration changed")
                disabled = [name for name in cast(list[object], raw_disabled) if name != server]
                servers = dict(cast(dict[str, object], raw_servers))
                if entry.direction == "off":
                    definitions = dict(baseline.global_definitions)
                    for baseline_project in baseline.projects:
                        if baseline_project.path == project:
                            definitions.update(baseline_project.definitions)
                    servers[server] = definitions[server]
                else:
                    disabled.append(server)
                    _ = servers.pop(server, None)
                local["disabledMcpServers"] = disabled
                local["mcpServers"] = servers
        repaired = json.dumps(configuration, indent=2) + "\n"
        if isinstance(source, MissingMcpFile):
            if not atomic_file.create_text_atomic(config_file, repaired):
                raise AccountsError("accounts: MCP configuration changed during repair; rerun the command")
            return
        if config_file.read_text(encoding="utf-8") != source.text:
            raise AccountsError("accounts: MCP configuration changed during repair; rerun the command")
        backup = config_file.with_name(config_file.name + ".mcp-backup-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        if not atomic_file.create_text_atomic(backup, source.text):
            raise AccountsError("accounts: MCP backup already exists; no configuration changed")
        backup.chmod(source.mode)
        atomic_file.write_text_atomic(config_file, repaired)


def _existing_named_accounts() -> list[NamedAccount]:
    try:
        children = accounts_home().iterdir()
    except OSError:
        return []
    accounts: list[NamedAccount] = []
    for child in children:
        label = claude_config_label(child)
        if child.name.startswith(".") and re.fullmatch(r"claude[1-9][0-9]*", label):
            accounts.append(NamedAccount(label, child))
    return sorted(accounts, key=lambda account: _label_number(account.label))


def _status_for(config_dir: Path, folder: str, named: NamedAccount, problems: list[str],
                state_source: ObservedStateCause | NoObservedStateCause = NO_OBSERVED_STATE_CAUSE,
                note_source: ObservedStateCause | NoObservedStateCause = NO_OBSERVED_STATE_CAUSE) -> AccountStatus:
    mcp = _inspect_mcp_configuration(config_dir)
    # A named folder keeps its own label whatever login it holds; `problems` names a mismatch.
    label = named.label
    login = ""
    if mcp.account_is_safe_to_inspect:
        identity = account_identity(config_dir)
        if isinstance(identity, (agent_accounts.NumberedAccount, agent_accounts.UnnotedLogin)):
            login = identity.login
            if isinstance(identity, agent_accounts.UnnotedLogin):
                problems.append(unnoted_home_line(config_dir, identity.login))
        unclassified = _unclassified(config_dir)
    else:
        unclassified = ()
    return AccountStatus(
        folder=folder,
        config_dir=config_dir,
        is_named_account=True,
        is_set_up=True,
        account_is_safe_to_inspect=mcp.account_is_safe_to_inspect,
        label=label,
        login=login,
        role="",
        problems=tuple(problems),
        unclassified=unclassified,
        mcp_differences=mcp.differences,
        state_source=state_source,
        note_source=note_source,
        mcp_baseline_problems=mcp.baseline_problems,
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
            inspection = account_directory_readiness(label)
            statuses.append(_status_for(named.dir, folder, named, list(inspection.problems), inspection.state_source, inspection.note_source))
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
            mcp_differences=McpServerDifferences(),
        ))
    return statuses


def codex_statuses(workers_home: Path) -> list[CodexStatus]:
    """Every listed home, then unheld noted accounts whose home is not listed."""
    statuses: list[CodexStatus] = []
    homes = agent_accounts.codex_homes(workers_home)
    workers_login = _codex_home_login(workers_home).casefold()
    for home in homes:
        folder = _codex_folder(home)
        safe = not home.is_symlink() and not (home / "auth.json").is_symlink()
        login = _codex_home_login(home)
        if home == accounts_home() / ".codex" and not login:
            continue
        label = codex_home_label(home)
        match = re.fullmatch(r"\.codex([1-9][0-9]*)", home.name) if home.parent == accounts_home() else None
        inspection = (_inspect_codex_home(label, home, workers_home) if _label_is(label, "codex")
            else CodexHomeInspection(tuple(_check_default_codex_home(workers_home))) if home == accounts_home() / ".codex"
            else CodexHomeInspection(()))
        problems = list(inspection.problems)
        if login and not _label_is(label, "codex"):
            problems.append(unnoted_home_line(home, login))
        state_source = inspection.state_source
        if match and not login:
            intended = note_file.NumberedAccountLabel("codex", int(match.group(1))).label
            expected = _account_note(intended)
            hint = intended if isinstance(expected, ExpectedAccountLogin) else label
            diagnostic = f"accounts: sign in as {hint} at {folder}"
            problems.append(diagnostic)
            if isinstance(state_source, NoObservedStateCause):
                state_source = ObservedStateCause(diagnostic, ProblemCauseKey("signed out", CauseAccount(intended), CauseFile(str(home))))
        unclassified: tuple[str, ...] = ()
        if match and safe:
            try:
                unclassified = tuple(sorted(entry.name for entry in home.iterdir()
                    if entry.name not in CODEX_SHARED and entry.name not in CODEX_PRIVATE_NAMES))
            except OSError as error:
                problem = f"accounts: could not inspect {home}: {error}"
                if problem not in problems:
                    problems.append(problem)
        expected = _account_note(label) if _label_is(label, "codex") else ExpectedAccountLogin(login)
        note_source = _note_source(label, expected) if isinstance(expected, (MissingAccountNote, UnreadableAccountNote)) else NO_OBSERVED_STATE_CAUSE
        statuses.append(CodexStatus(
            folder, home, True, safe, NO_VALUE if label == login else label, login,
            CODEX_ROLE if home == workers_home or login and login.casefold() == workers_login else "spare", tuple(problems), unclassified, note_source=note_source, state_source=state_source,
        ))
    by_login: dict[str, int] = {}
    unique: list[CodexStatus] = []
    for status in statuses:
        key = status.login.casefold()
        if key and key in by_login:
            index = by_login[key]
            previous = unique[index]
            preferred = min((previous, status), key=lambda row: (
                row.home.name != f".{row.label}", row.home.name == ".codex", str(row.home)))
            unique[index] = replace(preferred,
                problems=tuple(dict.fromkeys((*previous.problems, *status.problems))),
                unclassified=tuple(sorted(set(previous.unclassified) | set(status.unclassified))))
        else:
            if key:
                by_login[key] = len(unique)
            unique.append(status)
    statuses = unique
    held_logins = set(by_login)
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
        inspection = (_inspect_codex_home(label, home, workers_home)
            if os.path.lexists(home) else CodexHomeInspection(()))
        statuses.append(CodexStatus(
            f"~/{home.name}", home, False, not home.is_symlink(), label, "", NO_VALUE, inspection.problems, (),
            state_source=inspection.state_source,
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


def _is_stale(usage: usage_store.StoredUsage | None, now: datetime, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> bool:
    account_config = read_account_settings(account_config)
    read = _read_at(usage)
    return read is None or now - read > timedelta(minutes=account_config.whole("usage_stale_minutes"))


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
    source: UsageSource = REFRESH_STALE_USAGE, *, problems: list[StatusProblem], represented_stderr: set[str],
 account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> dict[str, usage_store.StoredUsage]:
    """Each login's stored readings. A login with none read lately is read live first, all at once."""
    account_config = read_account_settings(account_config)
    stored = usage_store.load()
    if isinstance(source, StoredUsageOnly):
        return stored
    stale = [
        status.config_dir for status in statuses
        if status.login and _is_stale(stored.get(usage_store.key("Claude", status.login)), now, account_config=account_config)
    ]
    stale_codex = [status.home for status in codex
                   if status.login and (_is_stale(stored.get(usage_store.key("Codex", status.login)), now, account_config=account_config)
                       or _credit_reading(stored.get(usage_store.key("Codex", status.login),
                           usage_store.StoredUsage()), now, account_config=account_config)[0] == "stale")]
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
            reports: list[agent_accounts.Report] = []
            for read, directory, login in reads:
                report = replace(read.result(), source=directory,
                    last_known_login=agent_accounts.KnownUsageLogin(login))
                reports.append(report)
                for warning in report.store_warnings:
                    _observe_status_problem(_usage_store_problem(report, warning, [*statuses, *codex], now),
                                            (agent_accounts.usage_store_warning_line(warning),))
        for report in reports:
            startup = report.codex_startup
            if isinstance(startup, agent_accounts.CodexStartupFailure):
                for index, status in enumerate(codex):
                    if status.home == startup.home:
                        codex[index] = replace(status, startup=startup, problems=(*status.problems, startup.problem))
        pending_warnings = [(report, warning) for report in reports for warning in report.store_warnings]
        for report, warning in pending_warnings:
            _observe_status_problem(_usage_store_problem(report, warning, [*statuses, *codex], now),
                                    (agent_accounts.usage_store_warning_line(warning),))
        represented_stderr.update(agent_accounts.usage_store_warning_line(warning) for _, warning in pending_warnings)
        try:
            usage_store.save(reports, now)
        except (OSError, ValueError) as error:
            # Retain the failure before recovery work, which can also fail.
            failed_saves = [
                (report, agent_accounts.UsageSaveFailed("; ".join(dict.fromkeys([
                    *(warning.reason for warning in report.store_warnings if isinstance(warning, agent_accounts.UsageSaveFailed)),
                    str(error)])))) for report in reports]
            for report, warning in failed_saves:
                _observe_status_problem(_usage_store_problem(report, warning, [*statuses, *codex], now),
                    included_raw=tuple(agent_accounts.usage_store_warning_line(prior) for prior in report.store_warnings
                                       if isinstance(prior, agent_accounts.UsageSaveFailed)))
            _ = usage_store.merge(stored, reports, now)
            pending_warnings = [(report, warning) for report, warning in pending_warnings
                                if isinstance(warning, agent_accounts.UsageClaimFailed)] + failed_saves
        else:
            diagnostics = _status_diagnostics.get()
            if isinstance(diagnostics, StatusDiagnostics):
                for report in reports:
                    diagnostics.recover_save(report)
            # A read failure after this point must not claim the good save failed.
            stored = usage_store.load()
            # The final save recovers a failed per-read save, but cannot restore a missed hold.
            pending_warnings = [(report, warning) for report, warning in pending_warnings
                                if isinstance(warning, agent_accounts.UsageClaimFailed)]
        for report, warning in pending_warnings:
            problem = _usage_store_problem(report, warning, [*statuses, *codex], now)
            problems.append(problem)
            _observe_status_problem(problem, (agent_accounts.usage_store_warning_line(warning),))
    return stored


def _usage_store_problem(report: agent_accounts.Report, warning: agent_accounts.UsageStoreWarning,
                         statuses: list[AccountStatus | CodexStatus], at: datetime) -> StatusProblem:
    directory = report.source
    subject = next((_status_subject(status) for status in statuses
        if (isinstance(directory, agent_accounts.ClaudeDirectorySource) and isinstance(status, AccountStatus)
            and status.config_dir == directory.directory)
        or (isinstance(directory, agent_accounts.CodexHomeSource) and isinstance(status, CodexStatus)
            and status.home == directory.home)), report.tool)
    claim_failed = isinstance(warning, agent_accounts.UsageClaimFailed)
    return StatusProblem(subject,
        "usage store read continued without a hold" if claim_failed else "usage store reading not saved",
        ProblemCause("usage claim" if claim_failed else "usage save",
            f"{warning.reason}; read at {at.astimezone(agent_accounts.user_zone()):%Y-%m-%d %H:%M %Z}",
            CauseFile(str(directory.directory if isinstance(directory, agent_accounts.ClaudeDirectorySource)
                          else directory.home if isinstance(directory, agent_accounts.CodexHomeSource)
                          else usage_store.store_file()))),
        ProblemStep("check the usage store file and its permissions; run accounts.py status again"))


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
class NoRoleCount:
    """A spare account has no role membership to count."""


@dataclass(frozen=True)
class RoleSessionCounts:
    """Distinct members of one role, grouped by activity."""

    busy: int
    idle: int
    paused: int
    exited: int

    @property
    def total(self) -> int:
        return self.busy + self.idle + self.paused + self.exited


class RoleSessionCountsDocument(TypedDict):
    busy: int
    idle: int
    paused: int
    exited: int
    total: int


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
    role_counts: tuple[RoleSessionCounts | NoRoleCount, ...] = ()
    role_counts_by_machine: dict[str, dict[str, RoleSessionCountsDocument]] = field(default_factory=dict)
    selected_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatusReading:
    rows: tuple[StatusRow, ...]
    notes: tuple[str, ...]
    detail_lines: tuple[str, ...]
    zone: tzinfo
    checked_at: datetime
    represented_stderr: tuple[str, ...] = ()
    session_inputs: tuple[str, ...] = ()
    upcoming_moves: tuple[str, ...] = ()


class StatusAccountDocument(TypedDict):
    label: str
    tool: Literal["claude", "codex"]
    roles: list[str]
    selected_roles: list[str]
    role_counts: list[RoleSessionCountsDocument | None]
    role_counts_by_machine: dict[str, dict[str, RoleSessionCountsDocument]]
    weekly_left_percent: float | None
    resets_at: str | None
    credits: float | Literal["unlimited"] | None
    use_credits: bool
    read_at: str | None
    read_ok: bool
    credits_in_use: bool


class StatusDocument(TypedDict):
    upcoming_moves: list[str]
    session_inputs: list[str]
    accounts: list[StatusAccountDocument]
    problems: list[str]


def _reading_is_old(read_at: datetime, now: datetime, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> bool:
    """Use one age limit for usage and credits; future readings are fresh."""
    account_config = read_account_settings(account_config)
    return now - read_at > timedelta(minutes=account_config.whole("usage_stale_minutes"))


def _credit_reading(
    usage: usage_store.StoredUsage, now: datetime,
 *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> tuple[str, float | Literal["unlimited"] | StatusUnavailable]:
    account_config = read_account_settings(account_config)
    credits = usage.get("credits")
    if credits is None:
        return ("unavailable", StatusUnavailable("unavailable"))
    read_at = _stored_time(credits["at"])
    if read_at is None:
        return ("unavailable", StatusUnavailable("unavailable"))
    if _reading_is_old(read_at, now, account_config=account_config):
        return ("stale", StatusUnavailable(f"stale since {read_at:%Y-%m-%d %H:%M %Z}"))
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
 *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> StatusRow:
    account_config = read_account_settings(account_config)
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
                on_credits, credits = _credit_reading(held, now, account_config=account_config)
    return StatusRow(status.label, tool, status.folder, login, roles,
        weekly, resets, credits, on_credits,
        tool == "codex" and isinstance(agent_accounts.codex_credits_setting(status.label), agent_accounts.CreditsOn),
        read_at, usage_store.latest_read_outcome(usage or usage_store.StoredUsage()) if login
        else agent_accounts.UsageReadOutcome.OK,
        _stored_time((usage or {}).get("attempted_at", "")) or StatusUnavailable())


def _status_row_cells(row: StatusRow, zone: tzinfo, checked_at: datetime, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> tuple[str, ...]:
    account_config = read_account_settings(account_config)
    weekly = row.weekly_left_percent
    reset = row.resets_at
    credits = row.credits
    read = row.read_at
    role_cell = _role_count_text(row, row.roles[0]) if row.roles else NO_VALUE if row.tool == "codex" else ""
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
        date_format = "%m-%d %H:%M %Z" if (failed or checked_at - read > timedelta(minutes=account_config.whole("usage_stale_minutes"))) and local.date() != checked_at.astimezone(zone).date() else "%H:%M %Z"
        read_cell = local.strftime(date_format)
    if failed:
        read_cell += "*"
    return (row.folder, row.label, role_cell,
        weekly_cell,
        reset.cell if isinstance(reset, StatusUnavailable) else reset.astimezone(zone).strftime("%a %m-%d %H:%M %Z"),
        row.on_credits,
        credits.cell if isinstance(credits, StatusUnavailable) else credits if isinstance(credits, str) else f"{credits:,.0f}",
        read_cell)


def render_status_json(reading: StatusReading, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> str:
    account_config = read_account_settings(account_config)
    accounts: list[StatusAccountDocument] = []
    for row in reading.rows:
        item = StatusAccountDocument(label=row.label, tool=row.tool, roles=list(row.roles), selected_roles=list(row.selected_roles),
            role_counts=[RoleSessionCountsDocument(busy=count.busy,
                idle=count.idle, paused=count.paused, exited=count.exited, total=count.total)
                if isinstance(count, RoleSessionCounts) else None for count in
                (row.role_counts or tuple(NoRoleCount() for _role in row.roles))],
            role_counts_by_machine=row.role_counts_by_machine,
            weekly_left_percent=None if isinstance(row.weekly_left_percent, (StatusUnavailable, WeeklyNotReadSince)) else row.weekly_left_percent,
            resets_at=None if isinstance(row.resets_at, StatusUnavailable) else row.resets_at.astimezone(timezone.utc).isoformat(),
            credits=None if isinstance(row.credits, StatusUnavailable) else row.credits,
            use_credits=row.use_credits,
            read_at=None if isinstance(row.read_at, StatusUnavailable) else row.read_at.astimezone(timezone.utc).isoformat(),
            read_ok=isinstance(row.read_at, datetime) and not _reading_is_old(row.read_at, reading.checked_at, account_config=account_config)
            and row.on_credits != "stale" and row.read_outcome in (
                agent_accounts.UsageReadOutcome.OK, agent_accounts.UsageReadOutcome.HELD),
            credits_in_use=row.on_credits == "yes")
        accounts.append(item)
    return json.dumps(StatusDocument(upcoming_moves=list(reading.upcoming_moves), session_inputs=list(reading.session_inputs), accounts=accounts, problems=[note.removeprefix("* ") for note in reading.notes]))


ProblemKind = Literal["warning", "usage claim", "usage save", "startup", "usage", "credits", "legacy", "unnoted", "test login", "access", "identity", "signed out", "wrong login", "unreadable note", "note", "state", "check", "role", "unclassified", "mcp servers"]


@dataclass(frozen=True)
class NoSeparateCause:
    """The fault itself states all that is known about its cause."""


@dataclass(frozen=True)
class NoCauseFile:
    """The cause does not name one file."""


@dataclass(frozen=True)
class CauseFile:
    path: str


NO_CAUSE_FILE = NoCauseFile()


@dataclass(frozen=True)
class DuplicateLoginHomes:
    """The two homes that share one sign-in, in a stable order."""

    homes: tuple[str, str]


@dataclass(frozen=True)
class UsageServiceFailure:
    """A service result belongs to its login, not to an identity file."""

    tool: str
    login: str
    outcome: agent_accounts.UsageReadOutcome


@dataclass(frozen=True)
class NoCauseAccount:
    """The cause does not name a numbered account."""


@dataclass(frozen=True)
class CauseAccount:
    label: str


@dataclass(frozen=True)
class OwnProblemCause:
    """This diagnostic describes its cause directly."""


@dataclass(frozen=True)
class McpServerCause:
    """One server and direction remain independent of other MCP faults."""

    server: str
    direction: McpDirection


@dataclass(frozen=True)
class ProblemCauseKey:
    """One cause on one account or home, shared by its symptoms."""

    kind: ProblemKind
    account: CauseAccount | NoCauseAccount
    location: CauseFile | DuplicateLoginHomes | UsageServiceFailure | McpServerCause | NoCauseFile


@dataclass(frozen=True)
class ProblemCause:
    kind: ProblemKind
    explanation: str
    location: CauseFile | DuplicateLoginHomes | UsageServiceFailure | McpServerCause | NoCauseFile = NoCauseFile()
    account: CauseAccount | NoCauseAccount = NoCauseAccount()
    key: ProblemCauseKey | OwnProblemCause = OwnProblemCause()


@dataclass(frozen=True)
class NoUserStep:
    """No separate user action is known or needed."""


@dataclass(frozen=True)
class ProblemStep:
    instruction: str


@dataclass(frozen=True)
class StatusProblem:
    """One subject, fault, cause and repair, before either status renderer."""

    subject: str
    wrong: str
    cause: ProblemCause | NoSeparateCause
    step: ProblemStep | NoUserStep
    footnote: bool = False
    origin: Literal["diagnostic", "account state", "role"] = "diagnostic"
    state_source: ObservedStateCause | NoObservedStateCause = field(default_factory=NoObservedStateCause)
    affected_roles: tuple[str, ...] = ()


def render_status_problem(problem: StatusProblem) -> str:
    if isinstance(problem.cause, ProblemCause) and isinstance(problem.cause.location, McpServerCause):
        line = f"{problem.subject} {problem.wrong}"
        return line + ": " + problem.step.instruction if isinstance(problem.step, ProblemStep) else line
    parts = [f"{problem.subject}: {problem.wrong}"]
    if isinstance(problem.cause, ProblemCause):
        parts.append(problem.cause.explanation)
    if problem.affected_roles:
        parts.append("affected roles: " + ", ".join(problem.affected_roles))
    if isinstance(problem.step, ProblemStep):
        parts.append(problem.step.instruction)
    return ("* " if problem.footnote else "") + "; ".join(parts)


def is_test_login(login: str) -> bool:
    """Recognize reserved test domains, including their subdomains."""
    _, separator, domain = login.rpartition("@")
    domain = domain.casefold().rstrip(".")
    return bool(separator) and any(domain == reserved or domain.endswith("." + reserved)
        for reserved in ("invalid", "test", "example", "localhost", "example.com", "example.net", "example.org"))


NO_STARTUP_FAILURE = agent_accounts.NoCodexStartupFailure()


def _read_failure_problem(row: StatusRow, zone: tzinfo,
                          startup: agent_accounts.CodexStartupStatus = NO_STARTUP_FAILURE, *, now: datetime, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> list[StatusProblem]:
    account_config = read_account_settings(account_config)
    def stamp(at: datetime) -> str:
        local = at.astimezone(zone)
        return local.strftime("%H:%M" if local.date() == now.astimezone(zone).date() else "%m-%d %H:%M")

    previous = (f"this shows the reading from {stamp(row.read_at)}"
                if isinstance(row.read_at, datetime) else "no reading is stored yet")
    last_attempt = (f"last read attempted at {stamp(row.attempted_at)}"
                    if isinstance(row.attempted_at, datetime) else "the attempt time is not recorded")
    match row.read_outcome:
        case agent_accounts.UsageReadOutcome.STALE_TOKEN | agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN:
            attempt = login_renewal_attempts().get(row.label)
            failed = attempt is not None and isinstance(attempt.result, LoginRenewalFailed)
            cause = ("no login token" if row.read_outcome == agent_accounts.UsageReadOutcome.NO_LOGIN_TOKEN
                     else "login token expired")
            if failed and attempt is not None:
                cause += f"; a probe session at {attempt.at.astimezone(zone):%H:%M} failed"
            step = "log in on it by hand" if failed else "start a session on it"
        case agent_accounts.UsageReadOutcome.REFUSED_401:
            cause = "the usage service did not accept this login"
            step = "start a session on it to check it"
        case agent_accounts.UsageReadOutcome.RATE_LIMITED_429:
            cause, step = f"the usage service is busy; {previous}", last_attempt
        case agent_accounts.UsageReadOutcome.CREDENTIALS_UNAVAILABLE:
            cause, step = "login credentials unavailable in this session", "refresh from a logged-in desktop session"
        case agent_accounts.UsageReadOutcome.ERROR:
            cause, step = f"the usage read failed; {previous}", last_attempt
        case agent_accounts.UsageReadOutcome.OK | agent_accounts.UsageReadOutcome.HELD:
            cause, step = "", ""
    subject = row.label if _label_is(row.label, "claude") or _label_is(row.label, "codex") else row.folder
    reading_problems: list[StatusProblem] = []
    if row.on_credits == "stale" and isinstance(row.credits, StatusUnavailable):
        reading_problems.append(StatusProblem(subject, "credits reading is old",
            ProblemCause("credits", row.credits.cell), ProblemStep("run accounts.py status to refresh usage"), True))
    if isinstance(startup, agent_accounts.CodexStartupFailure):
        reading_problems.append(StatusProblem(subject, "usage unavailable",
            ProblemCause("startup", f"Codex did not start at {agent_accounts.codex_home_display(startup.home)}; {previous}"),
            ProblemStep(f"run {startup.setup_command} and try again" + (f"; {step}" if step else "")), True))
        if row.read_outcome in (agent_accounts.UsageReadOutcome.ERROR, agent_accounts.UsageReadOutcome.OK,
                              agent_accounts.UsageReadOutcome.HELD):
            return reading_problems
    if row.read_outcome in (agent_accounts.UsageReadOutcome.OK, agent_accounts.UsageReadOutcome.HELD):
        if isinstance(row.read_at, datetime) and _reading_is_old(row.read_at, now, account_config=account_config):
            reading_problems.append(StatusProblem(subject, "usage reading is old",
                ProblemCause("usage", f"last good read at {stamp(row.read_at)}"),
                ProblemStep("run accounts.py status to refresh usage"), True))
        return reading_problems
    return [*reading_problems, StatusProblem(subject, "usage unavailable", ProblemCause("usage", cause), ProblemStep(step), True)]


def render_status_table(reading: StatusReading, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> str:
    account_config = read_account_settings(account_config)
    rows: list[tuple[str, ...]] = []
    for row in reading.rows:
        cells = _status_row_cells(row, reading.zone, reading.checked_at, account_config=account_config)
        rows.append(cells)
        rows.extend(tuple(_role_count_text(row, role) if index == 2 else "" for index in range(len(cells))) for role in row.roles[1:])
    return "\n".join([_render_table(rows), "",
        *reading.upcoming_moves, *(("",) if reading.upcoming_moves else ()), *reading.notes, *reading.detail_lines])


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


def _status_subject(status: AccountStatus | CodexStatus) -> str:
    return status.label if _label_is(status.label, "codex") or _label_is(status.label, "claude") else status.folder


def _status_steps(status: AccountStatus | CodexStatus) -> tuple[str, str]:
    setup = f"run accounts.py setup {status.label}"
    login = (f"run CODEX_HOME={shlex.quote(str(status.home))} codex login" if isinstance(status, CodexStatus)
             else f"run CLAUDE_CONFIG_DIR={shlex.quote(str(status.config_dir))} claude and /login")
    return setup, login


def _codex_login_command(home: Path) -> str:
    return f"env CODEX_HOME={shlex.quote(str(home))} codex login"


def status_check_problem(text: str, subject: str, *, setup: str, login: str,
                          access_paths: tuple[str, ...] = (), note_paths: tuple[str, ...] = ()) -> StatusProblem:
    """Convert command diagnostics at the status boundary; launches keep their words."""
    text = text.removeprefix("accounts: ")
    primary, separator, tail = text.partition("; ")
    step = ProblemStep(tail if separator else setup)
    if primary.startswith("cannot read role map from "):
        return StatusProblem("role map", "unavailable", ProblemCause("role", text),
            ProblemStep("restore access to natedev or its saved role map; run accounts.py status --stored"))
    for marker in (" has no account", " has no mapped account"):
        if primary.endswith(marker):
            role = primary.split(marker, 1)[0]
            return StatusProblem(role, "not configured", ProblemCause("role", "the role map selects no account"), step)
    if " is on " in primary and " on this machine" in primary:
        return StatusProblem(primary.split(" is on ", 1)[0], "cannot launch on this machine",
            ProblemCause("role", primary), step)
    read = re.fullmatch(r"could not (read|inspect) (.+?): (.+)", text, re.DOTALL)
    if read is not None:
        path = read[2]
        kind: ProblemKind = "unreadable note" if path in note_paths else "access" if path in access_paths else "check"
        return StatusProblem(subject, f"cannot {read[1]} {path}", ProblemCause(kind, read[3], CauseFile(path),
            CauseAccount(Path(path).stem) if path in note_paths else NoCauseAccount()),
            ProblemStep(f"check the file and its permissions; {setup}"))
    for path in access_paths:
        if primary == f"{path} is missing":
            return StatusProblem(subject, "not set up", ProblemCause("access", f"{path} is missing"), ProblemStep(setup))
        if primary in (f"{path} is not logged in", f"{path} has no readable sign-in"):
            return StatusProblem(subject, "not logged in", ProblemCause("signed out", f"{path} has no readable sign-in"), ProblemStep(login))
        if primary in (f"{path} is not a directory", f"{path} is a link"):
            return StatusProblem(subject, f"cannot use {path}", ProblemCause("access", primary),
                ProblemStep(f"use a real private directory; {setup}"))
    if " has an invalid oauthAccount identity" in primary:
        return StatusProblem(subject, "invalid sign-in", ProblemCause("identity", primary), ProblemStep(login))
    if " has no readable login in its frontmatter" in primary:
        path = primary.split(" has no readable login", 1)[0]
        return StatusProblem(subject, "not selectable", ProblemCause("unreadable note", primary,
            CauseFile(path), CauseAccount(Path(path).stem) if path in note_paths else NoCauseAccount()),
            ProblemStep("set login: in the account note, then " + setup))
    if " has no account note at " in primary:
        return StatusProblem(subject, "not selectable", ProblemCause("note", primary,
            account=CauseAccount(primary.split(" ", 1)[0])),
            ProblemStep("add the account note with its login, then " + setup))
    if primary.startswith("sign in as "):
        hint, _, folder = primary.removeprefix("sign in as ").rpartition(" at ")
        instruction = (primary if _label_is(hint, "codex") or _label_is(hint, "claude")
                       else f"sign in at {folder}; {login}")
        return StatusProblem(subject, "not logged in", ProblemCause("signed out", "the home has no readable sign-in"),
            ProblemStep(instruction))
    if " holds a different login from " in primary:
        return StatusProblem(subject, "wrong sign-in", ProblemCause("wrong login", primary), step)
    if " is logged in as " in primary:
        return StatusProblem(subject, "wrong sign-in", ProblemCause("wrong login", primary.split(" is logged in as ", 1)[0] + f" holds a different login from {subject}"),
            step if separator else ProblemStep(login))
    if " are logged in as " in primary or " hold " in primary:
        homes = primary.split(" are logged in as ", 1)[0].split(" hold ", 1)[0].split(" and ", 1)
        cause = " and ".join(homes) + f" store {subject}'s login; two homes on one login break token refresh"
        location = DuplicateLoginHomes((min(homes), max(homes))) if len(homes) == 2 else NoCauseFile()
        return StatusProblem(subject, "login is stored in two homes", ProblemCause("identity", cause, location),
            ProblemStep(tail if tail.startswith("keep ") else "keep each login in one home"))
    if primary.endswith(" is a link"):
        return StatusProblem(subject, "private file is linked", ProblemCause("check", text, CauseFile(primary.removesuffix(" is a link"))),
            ProblemStep("use a private file in this home; " + setup))
    if primary.startswith(subject + " "):
        primary = primary[len(subject) + 1:]
    return StatusProblem(subject, primary, NoSeparateCause(), step)


def _account_state_problems(status: AccountStatus | CodexStatus) -> list[StatusProblem]:
    if status.is_set_up and status.account_is_safe_to_inspect and status.login:
        return []
    setup, login = _status_steps(status)
    if not status.is_set_up:
        wrong, kind, cause, step = "not set up", "access", "no folder holds this account", setup
    elif not status.account_is_safe_to_inspect:
        wrong, kind, cause, step = "folder unavailable", "access", "its folder could not be read", setup
    else:
        wrong, kind, cause, step = "not logged in", "signed out", "no readable sign-in is stored", login
    return [StatusProblem(_status_subject(status), wrong, ProblemCause(cast(ProblemKind, kind), cause), ProblemStep(step), True, origin="account state",
        state_source=status.state_source)]


def _status_problems(status: AccountStatus | CodexStatus, *, detail: bool, zone: tzinfo) -> list[StatusProblem]:
    subject = _status_subject(status)
    directory = status.home if isinstance(status, CodexStatus) else status.config_dir
    identity = directory / "auth.json" if isinstance(status, CodexStatus) else agent_accounts.claude_config_file(directory)
    test_signin = status.account_is_safe_to_inspect and is_test_login(status.login)
    setup, login_step = _status_steps(status)
    unnoted = unnoted_home_line(directory, status.login)
    source = status.state_source
    note_source = status.note_source
    problems: list[StatusProblem] = []
    for text in status.problems:
        if text == unnoted or (isinstance(status, CodexStatus)
                and isinstance(status.startup, agent_accounts.CodexStartupFailure) and text == status.startup.problem):
            continue
        problem = status_check_problem(text, subject, setup=setup, login=login_step,
        access_paths=(str(directory), status.folder, str(identity)),
        note_paths=(str(note_file.note_path(status.label)),) if _label_is(status.label, "claude") or _label_is(status.label, "codex") else ())
        if isinstance(source, ObservedStateCause) and text == source.diagnostic and isinstance(problem.cause, ProblemCause):
            problem = replace(problem, cause=replace(problem.cause, key=source.key))
        if isinstance(note_source, ObservedStateCause) and text == note_source.diagnostic and isinstance(problem.cause, ProblemCause):
            problem = replace(problem, cause=replace(problem.cause, key=note_source.key))
        problems.append(problem)
    if status.login and not test_signin:
        problems = [replace(problem, step=ProblemStep(f"repair the account note for {subject} with its login; {setup}"))
                    if isinstance(problem.cause, ProblemCause) and problem.cause.kind == "unreadable note"
                    else replace(problem, step=ProblemStep(
                        f"add {_account_note_template('codex' if isinstance(status, CodexStatus) else 'claude')} with the login for {subject}; {setup}"))
                    if isinstance(problem.cause, ProblemCause) and problem.cause.kind == "note"
                    else problem for problem in problems]
    if unnoted in status.problems and not test_signin:
        tool = "codex" if isinstance(status, CodexStatus) else "claude"
        instruction = f"add {_account_note_template(tool)} with the login for {subject}"
        problems.append(StatusProblem(subject, "not selectable", ProblemCause("unnoted", "this login has no account note"),
            ProblemStep(instruction), state_source=note_source))
    if test_signin:
        # A test sign-in causes note advice, but not independent file faults.
        problems = [replace(problem, step=ProblemStep(problem.step.instruction.removesuffix("; " + setup)))
                    if isinstance(problem.step, ProblemStep) else problem
                    for problem in problems if not (isinstance(problem.cause, ProblemCause)
                        and problem.cause.kind in ("note", "unreadable note", "unnoted", "wrong login"))]
        problems.insert(0, test_login_problem(subject, identity, zone))
    else:
        problems.extend(_account_state_problems(status))
    if detail and status.unclassified:
        problems.append(StatusProblem(subject, "unclassified entries", ProblemCause("unclassified", ", ".join(status.unclassified)),
            ProblemStep(f"check these entries in {status.folder}; keep private files in this home")))
    if isinstance(status, AccountStatus):
        for difference in status.mcp_differences.entries:
            count = len(difference.projects)
            scopes = "project" if count == 1 else "projects"
            arguments = (status.label, difference.server)
            command = ("~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py mcp-repair "
                       + shlex.join(("--", *arguments) if any(value.startswith("-") for value in arguments) else arguments))
            problems.append(StatusProblem(subject,
                f"turns {difference.server} {difference.direction} in {count} {scopes} where ~/.claude does not",
                ProblemCause("mcp servers", ", ".join(difference.projects),
                    McpServerCause(difference.server, difference.direction)), ProblemStep(command)))
    bound: list[StatusProblem] = []
    for problem in problems:
        if isinstance(problem.cause, ProblemCause):
            kind: ProblemKind = problem.cause.kind
            problem = replace(problem, cause=replace(problem.cause,
                account=_status_cause_account(status), key=problem.cause.key if isinstance(problem.cause.key, ProblemCauseKey)
                else _status_cause_key(status, kind, problem.cause.location)))
        bound.append(problem)
    if isinstance(status, AccountStatus):
        for unreadable in status.mcp_baseline_problems:
            bound.append(StatusProblem("~/.claude", "cannot read MCP configuration",
                ProblemCause("mcp servers", f"{_codex_folder(unreadable.path)}: {unreadable.reason}", CauseFile(str(unreadable.path)),
                    key=ProblemCauseKey("mcp servers", NoCauseAccount(), CauseFile(str(unreadable.path)))),
                ProblemStep(f"check {_codex_folder(unreadable.path)} and its permissions")))
    return bound


def _status_cause_account(status: AccountStatus | CodexStatus) -> CauseAccount | NoCauseAccount:
    return CauseAccount(status.label) if _label_is(status.label, "claude") or _label_is(status.label, "codex") else NoCauseAccount()


def _status_cause_key(status: AccountStatus | CodexStatus, kind: ProblemKind,
                      specific: CauseFile | DuplicateLoginHomes | UsageServiceFailure | McpServerCause | NoCauseFile = NO_CAUSE_FILE) -> ProblemCauseKey:
    if not isinstance(specific, NoCauseFile):
        return ProblemCauseKey(kind, NoCauseAccount() if isinstance(specific, DuplicateLoginHomes)
                               else _status_cause_account(status), specific)
    location = (str(note_file.note_path(status.label)) if kind in ("note", "unreadable note", "unnoted")
                and isinstance(_status_cause_account(status), CauseAccount) else status.folder)
    return ProblemCauseKey(kind, _status_cause_account(status), CauseFile(str(Path(location).expanduser().absolute())))


def test_login_problem(subject: str, identity: Path, zone: tzinfo) -> StatusProblem:
    file = _codex_folder(identity)
    try:
        stamp = datetime.fromtimestamp(identity.stat().st_mtime, timezone.utc).astimezone(zone)
        written = f"at {stamp:%Y-%m-%d %H:%M %Z}"
    except OSError:
        written = "at an unknown time (the file time could not be read)"
    return StatusProblem(subject, "test sign-in is stored", ProblemCause("test login", f"a test wrote {file} {written}", CauseFile(file),
        CauseAccount(subject) if _label_is(subject, "claude") or _label_is(subject, "codex") else NoCauseAccount()),
        ProblemStep(f"delete {file}"))


def _problem_cause_key(problem: StatusProblem) -> ProblemCauseKey | NoSeparateCause:
    if not isinstance(problem.cause, ProblemCause):
        return NoSeparateCause()
    if isinstance(problem.cause.key, ProblemCauseKey):
        key = problem.cause.key
        if isinstance(key.location, CauseFile):
            return replace(key, location=CauseFile(str(Path(key.location.path).expanduser().absolute())))
        return key
    account = problem.cause.account
    if isinstance(account, NoCauseAccount) and isinstance(problem.cause.location, NoCauseFile):
        account = CauseAccount(problem.subject)
    location = problem.cause.location
    if isinstance(location, CauseFile):
        location = CauseFile(str(Path(location.path).expanduser().absolute()))
    if isinstance(location, DuplicateLoginHomes):
        account = NoCauseAccount()
    return ProblemCauseKey(problem.cause.kind, account, location)


def merge_status_problems(problems: list[StatusProblem]) -> list[StatusProblem]:
    """Merge only diagnostics with the same typed cause and owner."""
    groups: dict[ProblemCauseKey, list[StatusProblem]] = {}
    observation_order: list[StatusProblem | ProblemCauseKey] = []
    result: list[StatusProblem] = []
    direct_causes = {_problem_cause_key(problem) for problem in problems if isinstance(problem.state_source, NoObservedStateCause)}
    roles_by_cause: dict[ProblemCauseKey, list[str]] = {}
    steps_by_cause: dict[ProblemCauseKey, list[str]] = {}
    for problem in problems:
        if isinstance(problem.state_source, ObservedStateCause):
            roles_by_cause.setdefault(problem.state_source.key, []).extend(problem.affected_roles)
            if problem.affected_roles and isinstance(problem.step, ProblemStep):
                steps_by_cause.setdefault(problem.state_source.key, []).append(problem.step.instruction)
    for problem in problems:
        if isinstance(problem.state_source, ObservedStateCause) and problem.state_source.key in direct_causes:
            continue
        own_key = _problem_cause_key(problem)
        if isinstance(own_key, ProblemCauseKey) and own_key in roles_by_cause:
            problem = replace(problem, affected_roles=tuple(dict.fromkeys(roles_by_cause[own_key])))
            if isinstance(problem.step, ProblemStep):
                instruction = problem.step.instruction
                for step in dict.fromkeys(steps_by_cause.get(own_key, [])):
                    if step not in instruction:
                        instruction += "; " + step
                problem = replace(problem, step=ProblemStep(instruction))
        key = _problem_cause_key(problem)
        if isinstance(key, NoSeparateCause) or (problem.origin == "account state"
                and isinstance(problem.state_source, NoObservedStateCause)):
            observation_order.append(problem)
        else:
            if key not in groups:
                groups[key] = []
                observation_order.append(key)
            groups[key].append(problem)
    priority: tuple[ProblemKind, ...] = ("warning", "usage claim", "usage save", "test login", "startup", "unreadable note", "wrong login",
        "identity", "access", "signed out", "unnoted", "note", "legacy", "usage", "credits", "state", "check", "role", "unclassified", "mcp servers")
    for observed in observation_order:
        if isinstance(observed, StatusProblem):
            result.append(observed)
            continue
        related = groups[observed]
        def rank(problem: StatusProblem) -> tuple[int, int]:
            assert isinstance(problem.cause, ProblemCause)
            return (priority.index(problem.cause.kind), {"diagnostic": 0, "role": 1, "account state": 2}[problem.origin])
        chosen = min(related, key=lambda item: (rank(item), render_status_problem(item)))
        assert isinstance(chosen.cause, ProblemCause)
        if chosen.cause.kind == "signed out":
            steps = list(dict.fromkeys(problem.step.instruction for problem in related
                if problem.origin == "diagnostic" and isinstance(problem.step, ProblemStep)))
            if steps:
                chosen = replace(chosen, step=ProblemStep("; ".join(steps)))
        if any(problem.footnote and problem.origin != "account state" for problem in related):
            chosen = replace(chosen, footnote=True)
        result.append(chosen)
    # The same diagnostic can come from two inventory rows for one account.
    unique: dict[str, StatusProblem] = {}
    for problem in result:
        _ = unique.setdefault(render_status_problem(problem), problem)
    return list(unique.values())


def _usage_problem_identity(problem: StatusProblem, row: StatusRow, status: AccountStatus | CodexStatus) -> StatusProblem:
    """Only the producer's startup observation identifies a home fault."""
    assert isinstance(problem.cause, ProblemCause)
    key = (ProblemCauseKey("usage", _status_cause_account(status), UsageServiceFailure(row.tool, row.login, row.read_outcome))
           if problem.cause.kind == "usage" else _status_cause_key(status, problem.cause.kind))
    return replace(problem, cause=replace(problem.cause, key=key))


def _role_problem(error: AccountsError | str, note_paths: tuple[str, ...]) -> StatusProblem:
    problem = status_check_problem(str(error), "roles", setup="check the role map on natedev",
        login="check the selected account", note_paths=note_paths)
    source = error.source if isinstance(error, AccountsError) else NO_OBSERVED_STATE_CAUSE
    return replace(problem, origin="role", state_source=source,
        affected_roles=(problem.subject,) if isinstance(source, ObservedStateCause) and problem.subject != "roles" else ())


def _test_signin_role_advice(problem: StatusProblem, test_accounts: set[CauseAccount | NoCauseAccount]) -> bool:
    """A test sign-in must not receive account-note repair advice."""
    return (problem.origin == "role" and isinstance(problem.cause, ProblemCause)
            and problem.cause.account in test_accounts
            and problem.cause.kind in ("note", "unreadable note", "unnoted", "wrong login"))


def _read_status(source: UsageSource, *, detail: bool, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> StatusReading:
    """One table row per account folder, then one line for each thing wrong in a folder.

    With `detail`, also unknown entries, each live session, the Codex workers and recent moves.
    """
    account_config = read_account_settings(account_config)
    zone = agent_accounts.user_zone()
    statuses = account_statuses()
    for status in statuses:
        for problem in _status_problems(status, detail=detail, zone=zone):
            _observe_status_problem(problem)
    role_problems: list[AccountsError | str] = []
    try:
        selected_workers = role_account("workers")
        label = read_role_map().workers
        if not label:
            raise AccountsError("accounts: workers has no mapped account; run accounts.py set-role workers <codexN>")
        selected = agent_accounts.codex_account_home(label)
        if not isinstance(selected, agent_accounts.CodexHomeHoldingLogin):
            kind: ProblemKind = "signed out" if selected.home.is_dir() else "access"
            state = "not logged in" if selected.home.is_dir() else "not set up"
            step = (f"CODEX_HOME={shlex.quote(str(selected.home))} codex login" if selected.home.is_dir()
                    else f"run accounts.py setup {label}")
            diagnostic = f"accounts: workers is on {label}, which is {state} on this machine; {step}"
            raise AccountsError(diagnostic, source=ObservedStateCause(diagnostic,
                ProblemCauseKey(kind, CauseAccount(label), CauseFile(str(selected.home)))))
    except AccountsError as error:
        role_problems.append(error)
        _observe_status_problem(_role_problem(error, tuple(str(note_file.note_path(status.label)) for status in statuses)))
        selected_workers = CodexWorkersAccount("", accounts_home() / ".codex")
    codex = codex_statuses(selected_workers.home)
    if not selected_workers.label:
        codex = [replace(status, role="spare") if status.role == CODEX_ROLE else status for status in codex]
    for status in codex:
        for problem in _status_problems(status, detail=detail, zone=zone):
            _observe_status_problem(problem)
    now = datetime.now(timezone.utc)
    store_problems: list[StatusProblem] = []
    represented_stderr: set[str] = set()
    usage = _stored_usage(statuses, codex, now, source, problems=store_problems, represented_stderr=represented_stderr, account_config=account_config)
    for status in [*statuses, *codex]:
        tool = "Codex" if isinstance(status, CodexStatus) else "Claude"
        row = _status_row(status, usage.get(usage_store.key(tool, status.login)), now, (), account_config=account_config)
        for problem in _read_failure_problem(row, zone, status.startup if isinstance(status, CodexStatus) else NO_STARTUP_FAILURE, now=now, account_config=account_config):
            _observe_status_problem(_usage_problem_identity(problem, row, status))
    try:
        groups = group_accounts()
    except AccountsError as error:
        if isinstance(source, RefreshStaleUsage):
            raise
        if str(error) not in role_problems:
            role_problems.append(error)
            _observe_status_problem(_role_problem(error, tuple(str(note_file.note_path(status.label)) for status in [*statuses, *codex])))
        groups = GroupAccounts()
        codex = [replace(status, role=NO_VALUE) for status in codex]
    group_directories: dict[ClaudeGroup, Path] = {}
    for group in CLAUDE_GROUPS:
        label = group_label(groups, group)
        if isinstance(label, GroupNotConfigured):
            error = f"accounts: {group} has no account; run accounts.py move {group} <claudeN>"
            role_problems.append(error)
            _observe_status_problem(_role_problem(error, ()))
        if isinstance(label, str) and _label_is(label, "claude"):
            try:
                group_directories[group] = _claude_role_selection(group, label).dir
            except AccountsError as error:
                role_problems.append(error)
                _observe_status_problem(_role_problem(error, tuple(str(note_file.note_path(status.label)) for status in statuses)))
                # The folder's status reports its unreadable note without selecting it.
                continue
    rows = (
        *(_status_row(status, usage.get(usage_store.key("Claude", status.login)), now,
            tuple(group for group, directory in group_directories.items() if status.config_dir == directory), account_config=account_config)
          for status in statuses),
        *(_status_row(status, usage.get(usage_store.key("Codex", status.login)), now,
            (status.role,) if status.role in (CODEX_ROLE, "spare") else (), account_config=account_config) for status in codex),
    )
    local_sessions = _read_local_role_sessions(groups)
    rows, session_inputs = _count_role_sessions(rows, local_sessions)
    rows, count_problems = _machine_role_counts(rows, now, account_config=account_config)
    session_inputs = (*session_inputs, str(_session_counts_cache()))
    all_statuses: list[AccountStatus | CodexStatus] = [*statuses, *codex]
    diagnostics_by_status = [_status_problems(status, detail=detail, zone=zone) for status in all_statuses]
    diagnostics = [problem for local in diagnostics_by_status for problem in local]
    problems: list[StatusProblem] = list(store_problems)
    problems.extend(_map_provenance_problem(role_map.observed_map_provenance()))
    for row, status in zip(rows, all_statuses):
        for problem in _read_failure_problem(row, zone, status.startup if isinstance(status, CodexStatus) else NO_STARTUP_FAILURE, now=now, account_config=account_config):
            problems.append(_usage_problem_identity(problem, row, status))
    note_paths = tuple(str(note_file.note_path(status.label)) for status in all_statuses
                       if isinstance(_status_cause_account(status), CauseAccount))
    test_accounts = {_status_cause_account(status) for status in all_statuses if is_test_login(status.login)}
    for error in role_problems:
        problem = _role_problem(error, note_paths)
        if _test_signin_role_advice(problem, test_accounts):
            continue
        problems.append(problem)
    main_login = account.claude_account(main_config_dir())
    if main_login is not None and is_test_login(main_login.login):
        problems.append(test_login_problem("~/.claude", agent_accounts.claude_config_file(main_config_dir()), zone))
    codex_home = accounts_home() / ".codex"
    codex_login = _codex_home_login(codex_home)
    if codex_login:
        home_status = next((status for status in codex if status.home == codex_home), None)
        subject = next((_status_subject(status) for status in codex
                        if status.login.casefold() == codex_login.casefold()), "~/.codex")
        if home_status is None and is_test_login(codex_login):
            problems.append(test_login_problem(subject, codex_home / "auth.json", zone))
    problems.extend(diagnostics)
    for problem in problems:
        _observe_status_problem(problem)
    notes = [render_status_problem(problem) for problem in merge_status_problems(problems)]
    notes.extend(count_problems)
    detail_lines: tuple[str, ...] = ()
    if detail:
        journal = moves.read_journal()
        live_lines = ((*local_sessions.session_lines, *_workers_status_lines(now, selected_workers, local_sessions.continuation_lines, account_config=account_config))
                      if isinstance(source, RefreshStaleUsage) else ())
        detail_lines = (*(f"{row.label}: {_role_count_text(row, role)}" for row in rows for role in row.roles if role != "spare"), *live_lines,
            *_move_status_lines(moves.account_moves(journal), zone, account_config=account_config), *_routing_status_lines(journal, zone))
    return StatusReading(rows, tuple(notes), detail_lines, zone, now, tuple(represented_stderr), session_inputs)


@dataclass
class StatusDiagnostics:
    """The command owns diagnostics until its output has been delivered."""

    problems: list[StatusProblem] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    counterparts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    included_raw: dict[str, tuple[str, ...]] = field(default_factory=dict)
    delivered: set[str] = field(default_factory=set)
    recovered: set[str] = field(default_factory=set)

    def observe(self, problem: StatusProblem, raw: tuple[str, ...] = (), *, included_raw: tuple[str, ...] = ()) -> None:
        if isinstance(problem.cause, ProblemCause) and problem.cause.kind in ("usage claim", "usage save"):
            prior = next((item for item in self.problems if _problem_cause_key(item) == _problem_cause_key(problem)
                          and item.subject == problem.subject), None)
            if prior is not None:
                self.problems.remove(prior)
        if problem not in self.problems:
            self.problems.append(problem)
        if raw:
            self.counterparts[render_status_problem(problem)] = tuple(dict.fromkeys(raw))
        if included_raw:
            self.included_raw[render_status_problem(problem)] = tuple(dict.fromkeys(included_raw))

    def recover_save(self, report: agent_accounts.Report) -> None:
        self.recovered.update(agent_accounts.usage_store_warning_line(warning)
            for warning in report.store_warnings if isinstance(warning, agent_accounts.UsageSaveFailed))
        self.problems[:] = [problem for problem in self.problems if not (
            isinstance(problem.cause, ProblemCause) and problem.cause.kind == "usage save"
            and render_status_problem(problem) in self.counterparts
            and any(line in self.recovered for line in self.counterparts[render_status_problem(problem)]))]

    def retain_raw(self, text: str, *, delivered: bool) -> None:
        lines = [line for line in text.splitlines() if line.strip()]
        self.raw.extend(lines)
        if delivered:
            self.delivered.update(lines)

    def replay(self, *, final_error: str = "") -> None:
        represented = set(self.recovered)
        if final_error:
            represented.add(final_error)
        test_accounts = {problem.cause.account for problem in self.problems
                         if isinstance(problem.cause, ProblemCause) and problem.cause.kind == "test login"}
        problems = [problem for problem in self.problems if not _test_signin_role_advice(problem, test_accounts)]
        for problem in merge_status_problems(problems):
            line = render_status_problem(problem)
            counterparts = self.counterparts.get(line, ())
            if line != final_error and not any(raw in self.delivered for raw in counterparts):
                print(line, file=sys.stderr)
            represented.update(counterparts)
            represented.update(self.included_raw.get(line, ()))
        for line in dict.fromkeys(self.raw):
            if line not in represented and line not in self.delivered:
                print(line, file=sys.stderr)


@dataclass(frozen=True)
class NoStatusDiagnostics:
    """No status command is collecting observations."""


_status_diagnostics: ContextVar[StatusDiagnostics | NoStatusDiagnostics] = ContextVar(
    "status_diagnostics", default=NoStatusDiagnostics())


def _observe_status_problem(problem: StatusProblem, raw: tuple[str, ...] = (), *, included_raw: tuple[str, ...] = ()) -> None:
    diagnostics = _status_diagnostics.get()
    if isinstance(diagnostics, StatusDiagnostics):
        diagnostics.observe(problem, raw, included_raw=included_raw)


def _map_provenance_problem(provenance: role_map.RoleMapProvenance) -> list[StatusProblem]:
    if not isinstance(provenance, role_map.CachedAfterFetchFailure):
        return []
    stamp = provenance.at.astimezone(agent_accounts.user_zone()).strftime("%Y-%m-%d %H:%M %Z")
    cause = f"natedev fetch failed: {provenance.failure}"
    return [StatusProblem("role map", "using saved assignments",
        ProblemCause("role", f"map saved at {stamp}; {cause}", CauseFile(str(role_map.role_map_cache_file()))),
        ProblemStep("restore access to natedev; run accounts.py status again"))]


def _observe_map_provenance(provenance: role_map.RoleMapProvenance) -> None:
    for problem in _map_provenance_problem(provenance):
        _observe_status_problem(problem, (role_map.cached_map_warning(provenance),)
            if isinstance(provenance, role_map.CachedAfterFetchFailure) else ())


class DiagnosticStream(io.StringIO):
    """Capture complete diagnostics while preserving terminal delivery."""

    def __init__(self, terminal: TextIO) -> None:
        super().__init__()
        self.terminal: TextIO = terminal

    @override
    def write(self, text: str) -> int:
        if self.terminal.isatty():
            _ = self.terminal.write(text)
        return super().write(text)

    @override
    def isatty(self) -> bool:
        return self.terminal.isatty()


@role_map.role_map_command
def read_status(*, source: UsageSource = REFRESH_STALE_USAGE, detail: bool = False, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> StatusReading:
    """Read each account once; retain diagnostics before any later failure."""
    account_config = read_account_settings(account_config)
    enclosing = _status_diagnostics.get()
    diagnostics = enclosing if isinstance(enclosing, StatusDiagnostics) else StatusDiagnostics()
    token = _status_diagnostics.set(diagnostics)
    tty = sys.stderr.isatty()
    held = DiagnosticStream(sys.stderr)
    try:
        with (redirect_stderr(held), role_map.observe_map_reads(_observe_map_provenance),
              role_map.stored_role_map() if isinstance(source, StoredUsageOnly) else nullcontext()):
            reading = _with_upcoming_moves(_read_status(source, detail=detail, account_config=account_config))
    except BaseException:
        diagnostics.retain_raw(held.getvalue(), delivered=tty)
        if not isinstance(enclosing, StatusDiagnostics):
            diagnostics.replay()
        raise
    finally:
        _status_diagnostics.reset(token)
    diagnostics.retain_raw(held.getvalue(), delivered=tty)
    for problem in _map_provenance_problem(role_map.observed_map_provenance()):
        diagnostics.observe(problem)
    represented = set(reading.represented_stderr) | diagnostics.recovered
    represented.update(raw for raw_lines in diagnostics.counterparts.values() for raw in raw_lines)
    represented.update(raw for raw_lines in diagnostics.included_raw.values() for raw in raw_lines)
    notes = list(reading.notes)
    for line in dict.fromkeys(held.getvalue().splitlines()):
        if not line.strip() or line in represented or line in diagnostics.delivered:
            continue
        mcp = re.fullmatch(r"~/(\.claude[0-9]+): mcpServers: .*", line)
        if mcp and any(note.startswith(mcp[1][1:] + " turns ") and " where ~/.claude does not: " in note for note in notes):
            continue
        notes.append(render_status_problem(StatusProblem("accounts", "read warning",
            ProblemCause("warning", line), NoUserStep())))
    return replace(reading, notes=tuple(dict.fromkeys(notes)))


def _upcoming_move_lines() -> tuple[str, ...]:
    try:
        if not role_map.is_role_map_host():
            return role_map.read_role_map().upcoming_moves or (
                f"Upcoming moves: unavailable; {role_map.ROLE_MAP_HOST} unreachable",)
        upcoming = next_moves.list_next_moves()
        return ("Upcoming moves: none",) if upcoming == "nothing set up" else ("Upcoming moves:", *upcoming.splitlines())
    except role_map.RoleMapUnavailable:
        return (f"Upcoming moves: unavailable; {role_map.ROLE_MAP_HOST} unreachable",)
    except (next_moves.NextMoveError, ValueError) as error:
        return (f"Upcoming moves: unavailable from {role_map.ROLE_MAP_HOST}; {str(error).splitlines()[0]}",)


def _with_upcoming_moves(reading: StatusReading) -> StatusReading:
    return replace(reading, upcoming_moves=_upcoming_move_lines())


def status_report(*, detail: bool = False, source: UsageSource = REFRESH_STALE_USAGE, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> str:
    """Show upcoming moves between the account table and its problems."""
    account_config = read_account_settings(account_config)
    reading = read_status(source=source, detail=detail, account_config=account_config)
    return render_status_table(reading, account_config=account_config)


# Run folders under unit/, and under delegate/ for a run started before the rename.


@dataclass(frozen=True)
class LocalRoleSessions:
    """One local observation supplies counts and detail output."""

    counts: dict[tuple[Path, str], dict[str, str]]
    members: dict[ClaudeGroup, list[LiveGroupMember]]
    inputs: tuple[str, ...]
    session_lines: tuple[str, ...]
    continuation_lines: tuple[str, ...]


@dataclass(frozen=True)
class CodexRoleProcess:
    pid: int
    activity: Literal["busy", "idle"]


def _local_codex_processes() -> dict[str, CodexRoleProcess]:
    """Read native Codex conversations and pane activity once for this board."""
    from scripts.herdr import herdr

    try:
        agents = herdr.Herdr(herdr.shared_session()).agents()
    except (herdr_types.HerdrFailure, OSError, ValueError, subprocess.SubprocessError):
        agents = []
    activities: dict[str, Literal["busy", "idle"]] = {
        agent["agent_session"].session_id: "busy" if agent["agent_status"] == herdr.AgentStatus.WORKING else "idle"
        for agent in agents if agent.get("agent") == "codex"
        and isinstance(agent["agent_session"], herdr.ReportedAgentSession)
    }
    wanted = next(iter(activities), "")
    return {identity: CodexRoleProcess(pids[0], activities.get(identity, "idle"))
            for identity, pids in herdr.codex_conversations(wanted=wanted).live.items() if pids}


def _session_config_dir(pid: int) -> account.ConfiguredClaudeFolder | account.NoAccountSetting | account.ProcessEnvironmentUnread:
    """Read the account setting without a subprocess for each Mac member."""
    if sys.platform != "darwin":
        return account.process_config_dir(pid)
    try:
        parts = account.procargs2_parts(account.kern_procargs2(pid), lambda name: name == "CLAUDE_CONFIG_DIR")
    except (OSError, ValueError):
        return account.ProcessEnvironmentUnread("process account could not be read")
    directory = parts.environment.get("CLAUDE_CONFIG_DIR", "")
    return account.ConfiguredClaudeFolder(Path(directory)) if directory else account.NoAccountSetting()


def _local_session_counts() -> tuple[dict[tuple[Path, str], dict[str, str]], tuple[str, ...]]:
    with role_map.stored_role_map():
        observed = _read_local_role_sessions(group_accounts())
    return observed.counts, observed.inputs


def _moving_role_suffix(role: str, label: str, groups: GroupAccounts) -> str:
    if role in CLAUDE_GROUPS:
        selected = group_label(groups, role)
        if isinstance(selected, str) and selected != label:
            return f"; moving to {selected}"
    return ""


@settings_report_scope()
def _read_local_role_sessions(groups: GroupAccounts, *, include_workers: bool = True) -> LocalRoleSessions:
    """Read local membership, processes, and activity for counts and moves."""
    from scripts.message import sessions
    from scripts.herdr import pane_launch
    from scripts.production import session_staff, staff

    inputs = {sessions.sessions_dir(), pane_launch.record_directory()}
    counts: dict[tuple[Path, str], dict[str, str]] = {}
    launches: dict[str, pane_launch.PaneLaunch] = {}
    try:
        membership = staff.local_snapshot()
    except staff.StaffUnreadable as error:
        if not isinstance(error.__cause__, FileNotFoundError):
            print(f"accounts: unit counts are incomplete: {error}", file=sys.stderr)
        membership = staff.StaffSnapshot(())
    for path in pane_launch.record_directory().glob("*.json"):
        inputs.add(path)
        try:
            entries = pane_launch.read_valid_entries(path, record=membership)
        except (OSError, ValueError):
            continue
        for launch in entries.values():
            previous = launches.get(launch["session_id"])
            if previous is None or previous["written_at"] < launch["written_at"]:
                launches[launch["session_id"]] = launch
    inputs.update(Path(path) for path in membership.inputs)
    machine = _session_counts_machine()

    def local_member(member: staff.StaffRow) -> bool:
        owner = (membership.parent_of(member) or member) if member.kind == "worker" else member
        if isinstance(owner.doc, staff.LiveDoc):
            return owner.doc.machine == machine
        location = owner.worktree
        return not isinstance(location, staff.PresentWorktree) or location.machine == machine

    for path in sessions.sessions_dir().glob("*.json"):
        inputs.add(path)
    live = {session["sessionId"]: session for session in sessions.running_sessions()}
    session_lines: list[str] = []
    members: dict[ClaudeGroup, list[LiveGroupMember]] = {"units": [], "showrunners": []}
    for identity, session in live.items():
        owner = membership.by_current_session(identity)
        candidates = [launch for reference, launch in launches.items()
                      if owner is not None and reference in (owner.id, owner.current_session)]
        launch = max(candidates, key=lambda launch: launch["written_at"]) if candidates else launches.get(identity)
        role = ""
        try:
            member = session_staff.unit_match(session_staff.ObservedUnit(""), membership,
                                             session_staff.LiveSession(identity))
            if isinstance(member, session_staff.NoSuchMember) and launch is not None:
                member = session_staff.unit_match(session_staff.ObservedUnit(""), membership,
                                                 session_staff.StoredReference(launch["session_id"]))
        except (staff.StaffUnreadable, ValueError) as error:
            print(f"accounts: unit {identity}: {error}", file=sys.stderr)
            continue
        if isinstance(member, session_staff.MemberFound):
            role = "units"
        if owner is not None and owner.kind in ("showrunner", "worker"):
            role = "showrunners" if owner.kind == "showrunner" else "workers"
        if owner is not None and owner.kind == "producer":
            settings = producer_settings(owner)
            role = ("showrunners" if isinstance(settings, models_registry.LaunchSettings) and settings.kind == "claude"
                    else "")
        setting = _session_config_dir(session["pid"])
        directory = setting.path if isinstance(setting, account.ConfiguredClaudeFolder) else None
        label = claude_config_label(directory) if directory is not None else "account unreadable"
        suffix = _moving_role_suffix(role, label, groups)
        session_lines.append(f"session {identity} ({session['name']}): {label}{suffix}")
        if role in CLAUDE_GROUPS and directory is not None:
            group = role
            members[group].append(LiveGroupMember(identity, session["name"], group, directory))
        if role and directory is not None:
            if role == "workers":
                state = "busy" if session.get("status") == "busy" else "idle"
            else:
                member_row = member.row if isinstance(member, session_staff.MemberFound) else owner
                state = session_state.classify(
                    member_row if member_row is not None else session_state.UnrecordedStaff(),
                    session_state.LiveActivity(session.get("status", "")))
            counts.setdefault((directory, role), {})[identity] = state
    # A member without a live session belongs to its selected role account.
    for member in (*staff.active(membership, "unit"), *staff.active(membership, "showrunner"),
                   *(row for row in staff.active(membership, "producer")
                     if isinstance(settings := producer_settings(row), models_registry.LaunchSettings)
                     and settings.kind == "claude")):
        if member.current_session in live or not local_member(member):
            continue
        role = "units" if member.kind == "unit" else "showrunners"
        selected = group_label(groups, role)
        if not isinstance(selected, str):
            continue
        directory = account_dir(selected)
        state = session_state.classify(member, session_state.NoLiveSession())
        counts.setdefault((directory, role), {})[str(member.id)] = state
        session_lines.append(f"session {member.current_session} ({member.name}): {selected}; {state}")
    observed_workers = set(live)
    if include_workers and (parents := session_staff.pane_parents(membership)):
        worker_panes = cast(AccountPaneReader, cast(object, import_module("scripts.herdr.worker_panes")))

        for states in worker_panes.observe_parents(parents, membership).values():
            for state in states:
                evidence = session_staff.worker_backend(membership, state.row.id)
                if not isinstance(evidence, session_staff.RecordedWorker):
                    session_lines.append(f"worker {state.row.name}: {evidence.reason}")
                    continue
                status = worker_panes.worker_state(state)
                if status == "unknown":
                    reason = cast(object, getattr(state, "reason", "pane status is unknown"))
                    session_lines.append(f"worker {state.row.name}: {reason}")
                    continue
                if isinstance(state, worker_panes.OpenWorker) and isinstance(evidence.backend, staff.CodexBackend):
                    directory = evidence.backend.codex_home
                    counts.setdefault((directory, "workers"), {})[state.row.current_session] = status
                    observed_workers.add(state.row.current_session)
    unobserved_workers = [member for member in staff.active(membership, "worker")
        if include_workers and member.id not in observed_workers and member.current_session not in observed_workers
        and isinstance(membership.backends.get(member.id), staff.CodexBackend) and local_member(member)]
    if unobserved_workers:
        codex_processes = _local_codex_processes()
        for member in unobserved_workers:
            if process := codex_processes.get(member.current_session):
                setting = account.process_codex_home(process.pid)
                if isinstance(setting, account.ProcessEnvironmentUnread):
                    continue
                directory = setting.home if isinstance(setting, account.ProcessCodexHome) else accounts_home() / ".codex"
                counts.setdefault((directory, "workers"), {})[member.current_session] = process.activity
    return LocalRoleSessions(counts, members, tuple(str(path) for path in sorted(inputs)),
                             tuple(session_lines), ())


def _count_role_sessions(rows: tuple[StatusRow, ...], observed: LocalRoleSessions) -> tuple[tuple[StatusRow, ...], tuple[str, ...]]:
    """Keep selected roles and each role observed on this account."""
    counted: list[StatusRow] = []
    for row in rows:
        directory = Path(row.folder).expanduser()
        roles = tuple(dict.fromkeys((*row.roles, *(role for folder, role in observed.counts if folder == directory))))
        if any(role != "spare" for role in roles):
            roles = tuple(role for role in roles if role != "spare")
        counted.append(replace(row, roles=roles, selected_roles=tuple(role for role in row.roles if role != "spare"),
            role_counts=tuple(NoRoleCount() if role == "spare" else _role_session_counts(
                tuple(observed.counts.get((directory, role), {}).values()), row.tool) for role in roles)))
    return tuple(counted), observed.inputs


class MachineSessionCounts(TypedDict):
    machine: str
    at: str
    accounts: dict[str, dict[str, RoleSessionCountsDocument]]


def _session_counts_cache() -> Path:
    return accounts_home() / ".local/state/accounts/peer_session_counts.json"


def _session_counts_machine() -> str:
    """Use the workstation alias paired with the shared peer resolver."""
    return "mac" if remote.other_machine() == "natedev" else "natedev"


def _session_counts_document(counts: dict[tuple[Path, str], dict[str, str]]) -> MachineSessionCounts:
    mapped: dict[tuple[str, str], dict[str, str]] = {}
    for (directory, role), members in counts.items():
        label = directory.name.removeprefix(".")
        if re.fullmatch(r"codex(?:[1-9][0-9]*)?", label):
            label = codex_home_label(directory)
        if not re.fullmatch(r"(?:claude|codex)[1-9][0-9]*", label):
            continue
        mapped.setdefault((label, role), {}).update(members)
    values: dict[str, dict[str, RoleSessionCountsDocument]] = {}
    for (label, role), members in mapped.items():
        tool = "codex" if label.startswith("codex") else "claude"
        count = _role_session_counts(tuple(members.values()), tool)
        values.setdefault(label, {})[role] = RoleSessionCountsDocument(busy=count.busy,
            idle=count.idle, paused=count.paused, exited=count.exited, total=count.total)
    document = MachineSessionCounts(machine=_session_counts_machine(),
        at=datetime.now(timezone.utc).isoformat(), accounts=values)
    return document


def _decode_session_counts(text: str, machine: str) -> MachineSessionCounts:
    value = cast(object, json.loads(text))
    if not isinstance(value, dict):
        raise ValueError("invalid session counts")
    fields = cast(dict[str, object], value)
    stamp, accounts = fields.get("at"), fields.get("accounts")
    if fields.get("machine") != machine or not isinstance(stamp, str) or _stored_time(stamp) is None or not isinstance(accounts, dict):
        raise ValueError("invalid session counts")
    checked: dict[str, dict[str, RoleSessionCountsDocument]] = {}
    for label, roles in cast(dict[str, object], accounts).items():
        if not re.fullmatch(r"(?:claude|codex)[1-9][0-9]*", label) or not isinstance(roles, dict):
            raise ValueError("invalid account counts")
        checked[label] = {}
        for role, raw in cast(dict[str, object], roles).items():
            if role not in ("units", "showrunners", "workers") or not isinstance(raw, dict):
                raise ValueError("invalid role counts")
            count = cast(dict[str, object], raw)
            busy, idle, paused, exited, total = (count.get(key) for key in ("busy", "idle", "paused", "exited", "total"))
            if any(type(value) is not int for value in (busy, idle, paused, exited, total)):
                raise ValueError("invalid activity counts")
            busy, idle, paused, exited, total = (cast(int, value) for value in (busy, idle, paused, exited, total))
            if min(busy, idle, paused, exited, total) < 0 or busy + idle + paused + exited != total:
                raise ValueError("invalid count total")
            checked[label][role] = RoleSessionCountsDocument(busy=busy, idle=idle, paused=paused, exited=exited, total=total)
    return MachineSessionCounts(machine=machine, at=stamp, accounts=checked)


def _fetch_session_counts() -> int:
    """Fetch only the other machine's local counts through the shared transport."""
    try:
        machine = remote.other_machine()
        document = _decode_session_counts(role_map.ssh_role_map(("session-counts-read",), machine=machine), machine)
        atomic_file.write_text_atomic(_session_counts_cache(), json.dumps(document))
    except (OSError, ValueError, subprocess.SubprocessError):
        atomic_file.write_text_atomic(_session_counts_cache(), json.dumps({"unavailable": True}))
        return 1
    return 0


def _start_session_counts_fetch() -> bool:
    """Transfer a nonblocking lock to one detached fetch process."""
    try:
        path = _session_counts_cache().with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            _ = subprocess.Popen([str(SCRIPTS / "lib" / "py"), str(Path(__file__).resolve()),
                "session-counts-fetch", "--counts-lock", str(lock.fileno())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(lock.fileno(),))
    except OSError:
        return False
    return True


def _machine_role_counts(
    rows: tuple[StatusRow, ...], now: datetime,
 *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> tuple[tuple[StatusRow, ...], tuple[str, ...]]:
    account_config = read_account_settings(account_config)
    local, peer = _session_counts_machine(), remote.other_machine()
    try:
        document = _decode_session_counts(_session_counts_cache().read_text(), peer)
        stamp = _stored_time(document["at"])
        if stamp is None or _reading_is_old(stamp, now, account_config=account_config):
            raise ValueError("old counts")
        peer_accounts = document["accounts"]
        available = True
    except (OSError, ValueError):
        peer_accounts = {}
        available = False
    started = _start_session_counts_fetch()
    available = available and started
    if not available:
        peer_accounts = {}
    counted: list[StatusRow] = []
    for row in rows:
        local_counts = {role: RoleSessionCountsDocument(busy=count.busy,
            idle=count.idle, paused=count.paused, exited=count.exited, total=count.total)
            for role, count in zip(row.roles, row.role_counts) if isinstance(count, RoleSessionCounts)}
        machines = {local: local_counts}
        if available:
            machines[peer] = peer_accounts.get(row.label, {})
        roles = tuple(dict.fromkeys((*row.roles, *(role for counts in machines.values() for role in counts))))
        if any(role != "spare" for role in roles):
            roles = tuple(role for role in roles if role != "spare")
        combined: list[RoleSessionCounts | NoRoleCount] = []
        for role in roles:
            counts = [machine[role] for machine in machines.values() if role in machine]
            combined.append(NoRoleCount() if role == "spare" else RoleSessionCounts(
                busy=sum(count["busy"] for count in counts),
                idle=sum(count["idle"] for count in counts),
                paused=sum(count["paused"] for count in counts),
                exited=sum(count["exited"] for count in counts)))
        counted.append(replace(row, roles=roles, role_counts=tuple(combined), role_counts_by_machine=machines))
    return tuple(counted), (() if available else (f"{peer} unavailable: counts are {local} only",))


def _role_count_text(row: StatusRow, role: str) -> str:
    """Render the same combined counts in the table and detail output."""
    selected = " ✓" if role in row.selected_roles else ""
    if role == "spare":
        return role
    index = row.roles.index(role)
    count = row.role_counts[index] if index < len(row.role_counts) else NoRoleCount()
    if not isinstance(count, RoleSessionCounts):
        return role + selected
    activity = f"busy {count.busy}, idle {count.idle}, paused {count.paused}, exited {count.exited}"
    return f"{role}{selected} {count.total} ({activity})"


def _role_session_counts(states: tuple[str, ...], _tool: Literal["claude", "codex"]) -> RoleSessionCounts:
    """Treat unknown live activity as idle without changing role membership."""
    busy, paused, exited = (states.count(state) for state in ("busy", "paused", "exited"))
    return RoleSessionCounts(busy, len(states) - busy - paused - exited, paused, exited)


def _workers_status_lines(now: datetime, selected: CodexWorkersAccount, continuation_lines: tuple[str, ...], *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> list[str]:
    account_config = read_account_settings(account_config)
    exclude = frozenset({selected.label}) if _label_is(selected.label, "codex") else _NO_EXCLUSIONS
    chosen = pick("codex", now, exclude=exclude, keep=NoKeep(), account_config=account_config)
    destination = chosen.label if isinstance(chosen, PickedAccount) else no_room_line("codex", chosen)
    lines = [f"Codex workers: {selected.label}; on refusal: {destination}"] if selected.label else []
    lines.extend(continuation_lines)
    return lines


def _move_status_lines(entries: list[moves.MoveEntry], zone: tzinfo, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> list[str]:
    account_config = read_account_settings(account_config)
    lines: list[str] = []
    for entry in entries[-account_config.whole("move_status_count"):]:
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
    _ = commands.add_parser("trust", help="trust configured folders in every Claude home")
    _ = commands.add_parser("role-map-read")
    _ = commands.add_parser("session-counts-read")
    counts_fetch = commands.add_parser("session-counts-fetch")
    _ = counts_fetch.add_argument("--counts-lock", type=int, required=True)
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
    _ = next_add.add_argument("--else", dest="fallback_targets", action="append", default=[])
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
    mcp_command = commands.add_parser("mcp-projects", help="list projects where an account's MCP server differs")
    _ = mcp_command.add_argument("label")
    _ = mcp_command.add_argument("server")
    _ = mcp_command.add_argument("direction", choices=("off", "on"))
    mcp_fix = commands.add_parser("mcp-repair", help="match one account MCP server to ~/.claude")
    _ = mcp_fix.add_argument("label")
    _ = mcp_fix.add_argument("server")
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
    _ = move_command.add_argument("--follow-role", action="store_true", help=argparse.SUPPRESS)
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


def group_half_point_hold(group: ClaudeGroup, now: datetime, *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> next_moves.ResolvedHold:
    account_config = read_account_settings(account_config)
    label = group_label(group_accounts(), group)
    return (next_moves.resolve_hold(next_moves.HalfPointHold(), label, now, account_config=account_config) if isinstance(label, str)
            else next_moves.ResolvedHold(account_config.whole("hold_default_minutes"), next_moves.NoUsablePace()))


def validate_next_move_target(entry: next_moves.NextMove) -> None:
    if isinstance(entry, next_moves.ClaudeGroupMove):
        for target in (entry.to, *entry.fallback_targets):
            _ = claude_account_dir(target)
    elif isinstance(entry.to, next_moves.CodexWorkersTarget):
        _ = check_codex_target(entry.to.label, accounts_home() / ".codex", repair=False)
    elif isinstance(entry.to.account, next_moves.ClaudeWorkersTarget):
        _ = claude_workers_account(entry.to.account.label)


@role_map.role_map_command
def main(argv: list[str], *, account_config: conf.ConfigRead | ReadCurrentAccountSettings = CURRENT_ACCOUNT_SETTINGS) -> int:
    account_config = read_account_settings(account_config)
    parser = _parser()
    options = cast(CommandLine, parser.parse_args(argv))
    if options.as_json and options.detail:
        parser.error("--json and --detail cannot be used together")
    if hasattr(options, "label"):
        options.label = note_file.canonical_label(options.label)
    try:
        if options.action == "session-counts-read":
            counts, _inputs = _local_session_counts()
            print(json.dumps(_session_counts_document(counts)))
            return 0
        if options.action == "session-counts-fetch":
            try:
                return _fetch_session_counts()
            finally:
                os.close(options.counts_lock)
        if options.action == "trust":
            return trust_accounts()
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
                        **{"else": options.fallback_targets},
                        added_at=datetime.now(timezone.utc).isoformat())]))[0]
                    validate_next_move_target(candidate)
                _ = next_moves.add_next_move(options.group, options.percent, options.label,
                                             options.hold_mode, fallback_targets=tuple(options.fallback_targets), insertion=options.insertion)
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
                print(role_map.encode_role_map(replace(read_role_map(), upcoming_moves=_upcoming_move_lines())))
            else:
                write_role_map(role_map.decode_role_map_change(options.change))
            return 0
        if options.detail and options.action != "status":
            raise AccountsError("--detail goes only with no action")
        if options.action == "mcp-repair":
            try:
                mcp_repair(options.label, options.server)
            except (OSError, ValueError) as error:
                raise AccountsError(f"accounts: MCP repair failed: {error}") from error
            return 0
        if options.action == "mcp-projects":
            for path in mcp_projects(options.label, options.server, options.direction):
                print(path)
            return 0
        if options.action == "placement":
            print(placement_report(account_config=account_config))
            return 0
        if options.action == "exec":
            return exec_account(options.label, options.claude_args, probe=options.probe, account_config=account_config) or 0
        if options.action == "move":
            if options.session == "workers":
                if not options.label or options.source or options.target:
                    raise AccountsError("move workers requires <claudeN|codexN>")
                moved = move_workers(options.label, dry_run=options.dry_run, why=options.why,
                                     follow_role=options.follow_role, account_config=account_config)
            elif options.session in CLAUDE_GROUPS:
                if not options.label or options.source or options.target:
                    raise AccountsError("move units|showrunners requires <claudeN>")
                moved = move_group(options.session, options.label, dry_run=options.dry_run, why=options.why, alerts=options.alerts, account_config=account_config)
            else:
                if not options.label or options.source or options.target:
                    raise AccountsError("move requires <session-id|name> <claudeN>")
                moved = move(options.session, options.label, dry_run=options.dry_run, why=options.why, alerts=options.alerts, account_config=account_config)
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
                    print(_codex_login_command(home))
                    print("Keep CODEX_HOME on that command: codex login without it signs ~/.codex out and in; workers always use their mapped account.")
                return 1 if problems else 0
            destination = account_dir(options.label)
            print(_claude_login_command(destination))
            print("/login")
            return 1 if problems else 0
        if options.action == "check":
            problems = check(options.label)
            for problem in problems:
                print(problem)
            return 1 if problems else 0
        if options.action == "status":
            source: UsageSource = StoredUsageOnly() if options.stored else RefreshStaleUsage()
            diagnostics = StatusDiagnostics()
            diagnostic_scope = _status_diagnostics.set(diagnostics)
            tty = sys.stderr.isatty()
            held = DiagnosticStream(sys.stderr)
            try:
                with redirect_stderr(held):
                    rendered = (render_status_json(read_status(source=source, detail=options.detail, account_config=account_config), account_config=account_config)
                                if options.as_json else status_report(source=source, detail=options.detail, account_config=account_config))
                print(rendered)
            except BaseException as error:
                diagnostics.retain_raw(held.getvalue(), delivered=tty)
                final_error = (render_status_problem(status_check_problem(str(error), "accounts",
                    setup="check the account folders and role map", login="check the selected account"))
                    if isinstance(error, (AccountsError, next_moves.NextMoveError, role_map.RoleMapUnavailable,
                                          role_map.RoleMapWriteRefused)) else "")
                diagnostics.replay(final_error=final_error)
                raise
            finally:
                _status_diagnostics.reset(diagnostic_scope)
            if not tty:
                _ = sys.stderr.write(held.getvalue())
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
                return result.returncode if isinstance(result, WorkersSwapStarted) else 0
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
        if options.action == "status":
            problem = status_check_problem(str(error), "accounts", setup="check the account folders and role map", login="check the selected account")
            print(render_status_problem(problem), file=sys.stderr)
        else:
            print(error, file=sys.stderr)
        return 2 if options.action == "exec" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
