#!/usr/bin/env python3
"""Build and inspect Claude and Codex account directories; select Claude roles."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict, cast, overload
from importlib import import_module


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(SCRIPTS / "whoami"))

import account  # noqa: E402
import agent_accounts  # noqa: E402
import note_file  # noqa: E402
import usage_store  # noqa: E402

if TYPE_CHECKING:
    from scripts.shutdown.inventory import Session


class ManualMoveResume(Protocol):
    def resume_command(self, session: Session, target: str, source_label: str = "", why: str = "") -> str: ...


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

ALWAYS_LINKED = frozenset({"projects", "sessions"})
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
        ".last-cleanup",
    }
)
IDENTITY_ENTRIES = (".credentials.json", ".claude.json", "account-label")
IDENTITY_ENTRIES_NOT_SEEDED = (".credentials.json", "account-label")
LABEL_PATTERN = re.compile(r"claude ([1-9][0-9]*)")
CODEX_LABEL_PATTERN = re.compile(r"codex ([1-9][0-9]*)")
# What the Codex account does in a production.
CODEX_ROLE = "seats"
# A table cell with nothing to show: the folder has no login, or is not set up.
NO_VALUE = "—"
TABLE_HEADINGS = (
    "Folder", "Label", "Login", "Role", "5-hour left", "5-hour resets", "Weekly left", "Weekly resets",
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
    seats: str


class LegacyRoleChange(TypedDict):
    at: str
    role: str
    label: str


class RoleChange(LegacyRoleChange):
    account_label: str


HistoricalRoleChange = LegacyRoleChange | RoleChange


@dataclass(frozen=True)
class DefaultAccount:
    """The default Claude account, launched without a directory assignment."""


@dataclass(frozen=True)
class NamedAccount:
    """An isolated Claude account directory selected by its numbered label."""

    label: str
    dir: Path


@dataclass(frozen=True)
class CodexSeatsAccount:
    """The Codex home selected for every seat, including the default home."""

    label: str
    home: Path


ClaudeLaunchAccount = DefaultAccount | NamedAccount
AccountSelection = ClaudeLaunchAccount | CodexSeatsAccount


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
    startup: agent_accounts.CodexStartupStatus = field(default_factory=agent_accounts.NoCodexStartupFailure)


class CommandLine(argparse.Namespace):
    action: str = ""
    label: str = ""
    role: str = ""
    since: str = ""
    until: str = ""
    dry_run: bool = False
    claude_args: list[str] = []
    session: str = ""
    source: str = ""
    target: str = ""
    why: str = ""


@dataclass(frozen=True)
class AbsentAccountsConfigurationFile:
    """The role configuration did not exist before a requested change."""


@dataclass(frozen=True)
class ExistingAccountsConfigurationFile:
    """The role configuration contents that preceded a requested change."""

    contents: str


PreviousAccountsConfigurationFile = (
    AbsentAccountsConfigurationFile | ExistingAccountsConfigurationFile
)


@dataclass(frozen=True)
class PreviousRoleSelection:
    """The directory selection and exact configuration preceding a role write."""

    label: str
    configuration: PreviousAccountsConfigurationFile


class SwapDirectorOwner(TypedDict):
    kind: Literal["director"]
    session_id: str


class SwapNoDirector(TypedDict):
    kind: Literal["no director"]


class SwapUnitSession(TypedDict):
    kind: Literal["unit"]
    session_id: str
    name: str


class SwapSeatSession(TypedDict):
    kind: Literal["seat"]
    session_id: str
    name: str
    owner: SwapDirectorOwner | SwapNoDirector


class SwapOtherSession(TypedDict):
    kind: Literal["showrunner", "top-level"]
    session_id: str
    name: str


SwapSession = SwapUnitSession | SwapSeatSession | SwapOtherSession


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
class SeatsSwapDryRun:
    """The target is ready; these run notices describe the proposed move."""

    lines: tuple[str, ...]


@dataclass(frozen=True)
class SeatsSwapStarted:
    """The seats role has moved; live turns finish on their existing server."""

    lines: tuple[str, ...]


SeatsSwapOutcome = SeatsSwapDryRun | SeatsSwapStarted


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
    return main_config_dir() / "config" / "accounts.json"


def roles_history_file() -> Path:
    return accounts_home() / ".local" / "state" / "accounts" / "roles.jsonl"


def account_dir(label: str) -> Path:
    match = LABEL_PATTERN.fullmatch(label)
    if match is None:
        raise AccountsError(f"accounts: {label} is not a claude N label")
    return accounts_home() / f".claude{match.group(1)}"


def codex_account_home(label: str) -> Path:
    match = CODEX_LABEL_PATTERN.fullmatch(label)
    if match is None:
        raise AccountsError(f"accounts: {label} is not a codex N label")
    return accounts_home() / f".codex{match.group(1)}"


def _is_expected_link(path: Path, target: Path) -> bool:
    try:
        return path.is_symlink() and os.readlink(path) == str(target)
    except OSError:
        return False


def _real_entry_problem(path: Path, shared_home: str = "~/.claude") -> str:
    return (
        f"accounts: {path} is a real file; move it aside or merge it into {shared_home}"
    )


def _repair_link(path: Path, target: Path, shared_home: str = "~/.claude") -> list[str]:
    if path.is_symlink():
        if _is_expected_link(path, target):
            return []
        path.unlink()
    elif path.exists():
        return [_real_entry_problem(path, shared_home)]
    path.symlink_to(target, target_is_directory=target.is_dir())
    return []


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
    if CODEX_LABEL_PATTERN.fullmatch(label):
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
    source_dir = main_config_dir().absolute()
    for name in SHARED:
        target = source_dir / name
        if name in ALWAYS_LINKED and not target.exists():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                problems.append(f"accounts: could not create {target}: {error}")
                continue
        if name not in ALWAYS_LINKED and not os.path.lexists(target):
            path = destination / name
            try:
                if _is_expected_link(path, target):
                    path.unlink()
            except OSError as error:
                problems.append(f"accounts: could not remove {path}: {error}")
            continue
        try:
            problems.extend(_repair_link(destination / name, target))
        except OSError as error:
            problems.append(f"accounts: could not link {destination / name}: {error}")

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


def _login_problem(config_dir: Path) -> list[str]:
    if isinstance(account_identity(config_dir), agent_accounts.UnreadableClaudeAccount):
        return [f"accounts: {config_dir} is not logged in"]
    return []


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


def _account_notes_directory() -> Path:
    return Path(os.environ.get("AGENT_NOTES_DIR", str(accounts_home() / "rust" / "hanadocs" / "agents")))


def _account_note(label: str) -> AccountNote:
    path = _account_notes_directory() / f"{label}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return MissingAccountNote(f"accounts: {label} has no account note at {path}")
    except (OSError, UnicodeError) as error:
        return UnreadableAccountNote(f"accounts: could not read {path}: {error}")
    close = text.find("\n---", 3)
    if text.startswith("---\n") and close != -1:
        login = note_file.Note(path, label.partition(" ")[0], text, 4, close).get("login")
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
    selected = NamedAccount(label, account_dir(label))
    return _inspect_account_directory(selected, _account_note(label))


def _inspect_account_directory(selected: NamedAccount, expected: AccountNote) -> AccountDirectoryReadiness:
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
    links = [*identity_problems, *_link_problems(destination)]
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
                f"accounts: ~/.claude{label.removeprefix('claude ')} is logged in as {identity.login}, not {label}; see Log out and in in ~/.claude/docs/accounts.md"
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
    """Return every reason a named account is not fit to launch."""
    if label == "default":
        return _login_problem(main_config_dir())
    if CODEX_LABEL_PATTERN.fullmatch(label):
        return _check_codex_home(label, _seats_home())
    return list(account_directory_readiness(label).problems)


def _setup_codex_home(label: str) -> list[str]:
    destination = codex_account_home(label)
    expected = _account_note(label)
    if not isinstance(expected, ExpectedAccountLogin):
        return [expected.problem]
    for home in _codex_identity_homes(_seats_home()):
        if home != destination and _codex_home_login(home).casefold() == expected.login.casefold():
            return [
                f"accounts: cannot set up {label}: {_codex_folder(home)} already holds its login; "
                + "two homes on one login break token refresh"
            ]
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
    source = (accounts_home() / ".codex").absolute()
    for name in CODEX_SHARED:
        target = source / name
        path = destination / name
        try:
            if target.exists():
                problems.extend(_repair_link(path, target, "~/.codex"))
            elif _is_expected_link(path, target):
                path.unlink()
            elif os.path.lexists(path):
                problems.append(f"accounts: {path} has no shared entry in ~/.codex")
        except OSError as error:
            problems.append(f"accounts: could not link {path}: {error}")
    if not problems:
        print(
            f"Wait for {label} ({_codex_folder(destination)}) to finish its shared session index "
            + "and answer initialize; an interrupted index can hold startup for 15 minutes, "
            + f"then indexing must finish; limit {agent_accounts.CODEX_HOME_PREPARATION_SECONDS / 60:g} minutes.",
            flush=True,
        )
        prepared = asyncio.run(agent_accounts.prepare_codex_home(destination))
        if isinstance(prepared, agent_accounts.CodexHomePreparationFailed):
            problems.append(f"accounts: {prepared.problem}")
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


def _codex_identity_homes(seats_home: Path) -> list[Path]:
    """Check every listed home and any other home this process knows about."""
    homes = agent_accounts.codex_homes(seats_home)
    process_home = agent_accounts.codex_home()
    if process_home not in homes:
        homes.append(process_home)
    return homes


def _codex_folder(home: Path) -> str:
    return f"~/{home.relative_to(accounts_home())}" if home.is_relative_to(accounts_home()) else str(home)


def _check_codex_home(label: str, seats_home: Path) -> list[str]:
    destination = codex_account_home(label)
    folder = _codex_folder(destination)
    if destination.is_symlink():
        return [f"accounts: {folder} is a link; an account home must be a real directory"]
    if not destination.exists():
        return [f"accounts: {folder} is missing"]
    if not destination.is_dir():
        return [f"accounts: {folder} is not a directory"]
    problems = _shared_link_problems(
        destination, (accounts_home() / ".codex").absolute(), CODEX_SHARED, frozenset(),
    )
    problems.extend(_codex_private_link_problems(destination))
    try:
        unknown = sorted(entry.name for entry in destination.iterdir()
                         if entry.name not in CODEX_SHARED and entry.name not in CODEX_PRIVATE_NAMES)
        if unknown:
            problems.append(f"{folder}: unclassified: {', '.join(unknown)}")
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
        for home in _codex_identity_homes(seats_home):
            if home != destination and _codex_home_login(home).casefold() == login.casefold():
                problems.append(f"accounts: {folder} and {_codex_folder(home)} are logged in as {login}; two homes on one login break token refresh")
    return problems


def _check_default_codex_home(seats_home: Path) -> list[str]:
    home = accounts_home() / ".codex"
    if home.is_symlink():
        return ["accounts: ~/.codex is a link; use a real home"]
    if not home.is_dir():
        return ["accounts: ~/.codex is not a directory; set up the default home first"]
    login = _codex_home_login(home)
    if not login:
        return ["accounts: ~/.codex has no readable sign-in; sign in there before swapping"]
    return [
        f"accounts: ~/.codex and {_codex_folder(other)} hold {login}; keep each login in one home"
        for other in _codex_identity_homes(seats_home)
        if other != home and _codex_home_login(other).casefold() == login.casefold()
    ]


def _drop_named_account(environment: dict[str, str]) -> None:
    for name in account.NAMED_ACCOUNT_VARIABLES:
        _ = environment.pop(name, None)


def exec_account(label: str, arguments: list[str]) -> None:
    """Replace this process with Claude on the selected, safe directory."""
    if label == "unit":
        selected = role_account("unit")
    elif label == "default":
        selected = DefaultAccount()
    else:
        selected = NamedAccount(label, account_dir(label))
    environment = dict(os.environ)
    _ = environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    if isinstance(selected, NamedAccount):
        default = _directory_identity(main_config_dir()) if label != "unit" else ClaudeDirectoryLoggedOut()
        expected = _account_note(selected.label)
        default_login = isinstance(default, ClaudeDirectoryLogin) and isinstance(expected, ExpectedAccountLogin) and expected.login.casefold() == default.login.casefold()
        if not default_login:
            readiness = _inspect_account_directory(selected, expected)
            if isinstance(readiness, AccountDirectoryNotSetUp):
                raise AccountsError(
                    f'accounts: ~/.claude{selected.label.removeprefix("claude ")} is not set up; /showrunner:accounts setup "{selected.label}" first'
                )
            if not isinstance(readiness, (AccountDirectoryReady, AccountDirectoryLoggedOut)) or (
                "--resume" in arguments and isinstance(readiness, AccountDirectoryLoggedOut)
            ):
                raise AccountsError(readiness.problems[0])
            environment.update(account.named_account_environment(selected.dir))
        else:
            _drop_named_account(environment)
    else:
        if "--resume" in arguments:
            problems = check("default")
            if problems:
                raise AccountsError(problems[0])
        _drop_named_account(environment)
    try:
        os.execvpe("claude", ["claude", *arguments], environment)
    except OSError as error:
        raise AccountsError(f"accounts: could not start claude: {error}") from error


def _validate_role(role: str) -> None:
    if role not in {"unit", "seats"}:
        raise AccountsError(f"accounts: {role} is not a role; only unit and seats are supported")


def _read_accounts_configuration() -> AccountsConfiguration:
    path = accounts_config_file()
    if not path.exists():
        return AccountsConfiguration()
    try:
        decoded = _read_json_object(path)
    except (OSError, ValueError, TypeError) as error:
        raise AccountsError(f"accounts: could not read {path}: {error}") from error
    configuration = AccountsConfiguration()
    for role, directory in (("unit", account_dir), ("seats", codex_account_home)):
        if role not in decoded:
            continue
        label = decoded[role]
        if not isinstance(label, str):
            raise AccountsError(f"accounts: {path} has an invalid {role} account")
        _ = directory(label)
        if role == "unit":
            configuration["unit"] = label
        else:
            configuration["seats"] = label
    return configuration


def _seats_home() -> Path:
    """Convert an unreadable seats role into the account tool's error."""
    try:
        return agent_accounts.seats_home()
    except (OSError, ValueError) as error:
        raise AccountsError(str(error)) from error


@overload
def role_account(role: Literal["unit"]) -> ClaudeLaunchAccount: ...


@overload
def role_account(role: Literal["seats"]) -> CodexSeatsAccount: ...


@overload
def role_account(role: str) -> AccountSelection: ...


def role_account(role: str) -> AccountSelection:
    """Resolve a launch role to the default or one named account."""
    _validate_role(role)
    if role == "seats":
        home = _seats_home()
        label = "default" if home == accounts_home() / ".codex" else f"codex {home.name.removeprefix('.codex')}"
        return CodexSeatsAccount(label, home)
    configuration = _read_accounts_configuration()
    label = configuration.get("unit")
    if label is None:
        return DefaultAccount()
    return NamedAccount(label, account_dir(label))


def account_prefix(selected: AccountSelection) -> str:
    """Return the shell assignment for an already selected account."""
    if isinstance(selected, DefaultAccount):
        return ""
    if isinstance(selected, CodexSeatsAccount):
        return f"CODEX_HOME={shlex.quote(str(selected.home))}"
    return account.named_account_prefix(selected.dir)


def launch_prefix(role: str) -> str:
    """Return the shell assignment needed before launching this role."""
    return account_prefix(role_account(role))


def multiple_accounts_configured() -> bool:
    """Whether account configuration or a named account opts into role displays."""
    configuration = _read_accounts_configuration()
    return "unit" in configuration or bool(_existing_named_accounts())


def checked_named_accounts() -> list[NamedAccount]:
    """Every existing named account fit for a live reading or launch."""
    return [named for named in _existing_named_accounts() if not check(named.label)]


def account_identity(config_dir: Path) -> agent_accounts.KnownClaudeAccount | agent_accounts.UnreadableClaudeAccount:
    """Convert the whoami identity boundary into an explicit readable state."""
    identity = account.claude_account(config_dir)
    if identity is None:
        return agent_accounts.UnreadableClaudeAccount()
    return agent_accounts.KnownClaudeAccount(identity.login, identity.label)


def claude_account_selection() -> agent_accounts.ClaudeAccountSelection:
    """Resolve role identities and fit named accounts once for this run."""
    multiple = multiple_accounts_configured()
    default = account_identity(main_config_dir())
    selected = role_account("unit")
    named_accounts: list[agent_accounts.CheckedNamedClaudeAccount] = []
    for named in checked_named_accounts():
        identity = account_identity(named.dir)
        if isinstance(identity, agent_accounts.KnownClaudeAccount):
            named_accounts.append(agent_accounts.CheckedNamedClaudeAccount(
                named.label, named.dir, identity,
            ))
    unit: agent_accounts.KnownClaudeAccount | agent_accounts.UnreadableClaudeAccount = default
    if isinstance(selected, NamedAccount):
        unit = next(
            (named.identity for named in named_accounts if named.directory == selected.dir),
            agent_accounts.UnreadableClaudeAccount(),
        )
    return agent_accounts.ClaudeAccountSelection(default, unit, multiple, tuple(named_accounts))


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


def _unit_role_history(default_label: str) -> list[UnitRoleAssignment]:
    path = roles_history_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as error:
        raise AccountsError(f"accounts: could not read {path}: {error}") from error
    history: list[UnitRoleAssignment] = []
    note_labels: dict[str, str] = {"default": default_label} if default_label else {}
    for index, line in enumerate(lines, 1):
        try:
            decoded = cast(object, json.loads(line))
            if not isinstance(decoded, dict):
                raise ValueError("expected a JSON object")
            values = cast(dict[str, object], decoded)
            role = values.get("role")
            if role not in {"unit", "seats"}:
                raise ValueError("invalid role")
            label, stamp = values.get("label"), values.get("at")
            if not isinstance(label, str) or not isinstance(stamp, str):
                raise ValueError("invalid label or timestamp")
            if label != "default":
                _ = account_dir(label) if role == "unit" else codex_account_home(label)
            if role == "seats":
                _ = _iso_time(stamp, "role time")
                if "account_label" in values and not isinstance(values["account_label"], str):
                    raise ValueError("invalid account_label")
                continue
            if "account_label" in values:
                account_label = values["account_label"]
                if not isinstance(account_label, str) or not account_label:
                    raise ValueError("invalid account_label")
                wire: HistoricalRoleChange = RoleChange(
                    at=stamp, role="unit", label=label, account_label=account_label,
                )
            else:
                wire = LegacyRoleChange(at=stamp, role="unit", label=label)
                if label not in note_labels:
                    directory = main_config_dir() if label == "default" else account_dir(label)
                    identity = account_identity(directory)
                    if isinstance(identity, agent_accounts.UnreadableClaudeAccount):
                        raise ValueError(f"account identity for {label} is unreadable")
                    note_labels[label] = identity.label
                account_label = note_labels[label]
            history.append(UnitRoleAssignment(_iso_time(wire["at"], "role time"), account_label))
        except (ValueError, TypeError) as error:
            raise AccountsError(f"accounts: {path}:{index}: invalid role change: {error}") from error
    return sorted(history, key=lambda assignment: assignment.at)


def _historical_roles(
    label: str, at: datetime, default_label: str, history: list[UnitRoleAssignment],
    fallback_unit: str,
) -> tuple[agent_accounts.ClaudeRole, ...]:
    unit_label = default_label if history else fallback_unit
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
    selection = claude_account_selection()
    default = selection.default_account
    default_label = default.label if isinstance(default, agent_accounts.KnownClaudeAccount) else ""
    history = _unit_role_history(default_label)
    unit = selection.unit_account
    fallback_unit = unit.label if isinstance(unit, agent_accounts.KnownClaudeAccount) else ""
    totals: dict[tuple[str, tuple[agent_accounts.ClaudeRole, ...]], tuple[float, float]] = {}
    for label, observations in _quota_observations().items():
        for before, after in zip(observations, observations[1:]):
            start, end = max(before.at, since), min(after.at, until)
            if start >= end or after.remaining > before.remaining:
                continue
            seconds = (after.at - before.at).total_seconds()
            rate = (before.remaining - after.remaining) / seconds
            boundaries = [start, *(assignment.at for assignment in history if start < assignment.at < end), end]
            for left, right in zip(boundaries, boundaries[1:]):
                span = (right - left).total_seconds()
                if span <= 0:
                    continue
                roles = _historical_roles(label, left, default_label, history, fallback_unit)
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


def _append_role_change(role: str, label: str, account_label: str) -> None:
    path = roles_history_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    change = RoleChange(
        at=datetime.now(timezone.utc).isoformat(),
        role=role,
        label=label,
        account_label=account_label,
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        _ = output.write(json.dumps(change, sort_keys=True) + "\n")
    path.chmod(0o600)


def _previous_accounts_configuration_file(
    path: Path,
) -> PreviousAccountsConfigurationFile:
    if not path.exists():
        return AbsentAccountsConfigurationFile()
    return ExistingAccountsConfigurationFile(path.read_text(encoding="utf-8"))


def _restore_accounts_configuration_file(
    path: Path,
    previous: PreviousAccountsConfigurationFile,
) -> None:
    if isinstance(previous, AbsentAccountsConfigurationFile):
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, previous.contents)


@contextmanager
def _roles_lock() -> Generator[None]:
    path = accounts_config_file().with_suffix(".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_role(role: str, label: str, account_label: str) -> PreviousRoleSelection:
    """Change one role under the lock; failed history writes restore the file."""
    _validate_role(role)
    path = accounts_config_file()
    with _roles_lock():
        try:
            previous = _previous_accounts_configuration_file(path)
            configuration = _read_accounts_configuration()
            history = roles_history_file()
            size = history.stat().st_size if history.exists() else -1
            previous_label = configuration.get("unit", "default") if role == "unit" else configuration.get("seats", "default")
            if role == "unit":
                if label == "default":
                    _ = configuration.pop("unit", None)
                else:
                    _ = account_dir(label)
                    configuration["unit"] = label
            elif label == "default":
                _ = configuration.pop("seats", None)
            else:
                _ = codex_account_home(label)
                configuration["seats"] = label
            _atomic_write(path, json.dumps(configuration, indent=2, sort_keys=True) + "\n")
        except (OSError, ValueError) as error:
            raise AccountsError(f"accounts: could not write {path}: {error}") from error
        try:
            _append_role_change(role, label, account_label)
        except OSError as error:
            try:
                _restore_accounts_configuration_file(path, previous)
                if size < 0:
                    history.unlink(missing_ok=True)
                elif history.exists():
                    with history.open("r+b") as output:
                        _ = output.truncate(size)
            except OSError as restore_error:
                raise AccountsError(
                    f"accounts: could not record role change ({error}); "
                    + f"could not restore {path}: {restore_error}"
                ) from restore_error
            raise AccountsError(f"accounts: could not record role change: {error}") from error
    return PreviousRoleSelection(previous_label, previous)


def _write_unit_role(label: str, identity: agent_accounts.KnownClaudeAccount) -> PreviousRoleSelection:
    return _write_role("unit", label, identity.label)


def _restore_unit_role(previous: PreviousRoleSelection, account_label: str) -> None:
    """Undo the unit move without discarding a seats write made during shutdown."""
    with _roles_lock():
        path = accounts_config_file()
        configuration = _read_accounts_configuration()
        earlier = (cast(dict[str, object], json.loads(previous.configuration.contents))
                   if isinstance(previous.configuration, ExistingAccountsConfigurationFile) else {})
        if configuration.get("seats") == earlier.get("seats"):
            _restore_accounts_configuration_file(path, previous.configuration)
        else:
            if previous.label == "default":
                _ = configuration.pop("unit", None)
            else:
                configuration["unit"] = previous.label
            _atomic_write(path, json.dumps(configuration, indent=2, sort_keys=True) + "\n")
        _append_role_change("unit", previous.label, account_label)


def set_role(role: str, label: str) -> None:
    """Atomically select a readable account and preserve its note identity."""
    _validate_role(role)
    if role == "seats":
        home = _seats_home()
        target = check_codex_target(label, home)
        _ = _write_role(role, label, target.label)
        return
    if label == "default":
        identity = account_identity(main_config_dir())
    else:
        directory = account_dir(label)
        default = account_identity(main_config_dir())
        if isinstance(default, agent_accounts.KnownClaudeAccount) and label.casefold() == default.label.casefold():
            raise AccountsError(f"accounts: {label} is the default account; use default")
        identity = account_identity(directory)
        if (
            isinstance(default, agent_accounts.KnownClaudeAccount)
            and isinstance(identity, agent_accounts.KnownClaudeAccount)
            and identity.login.casefold() == default.login.casefold()
        ):
            raise AccountsError(f"accounts: {label} is the default account; use default")
    if isinstance(identity, agent_accounts.UnreadableClaudeAccount):
        raise AccountsError(f"accounts: account identity for {label} is unreadable")
    _ = _write_unit_role(label, identity)


def _status_object(value: object, place: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AccountsError(f"accounts: shutdown inventory {place} must be an object")
    return cast(dict[str, object], value)


def _status_string(values: dict[str, object], field: str, place: str, *, allow_empty: bool = False) -> str:
    value = values.get(field)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise AccountsError(f"accounts: shutdown inventory {place}.{field} must be a nonempty string")
    return value


def _status_list(values: dict[str, object], field: str, place: str) -> list[object]:
    value = values.get(field)
    if not isinstance(value, list):
        raise AccountsError(f"accounts: shutdown inventory {place}.{field} must be a list")
    return cast(list[object], value)


def _swap_session(value: object, place: str) -> SwapSession:
    values = _status_object(value, place)
    session_id = _status_string(values, "session_id", place)
    name = _status_string(values, "name", place, allow_empty=True) or session_id
    kind = values.get("kind")
    if kind == "unit":
        return SwapUnitSession(kind="unit", session_id=session_id, name=name)
    if kind in ("showrunner", "top-level"):
        return SwapOtherSession(kind=kind, session_id=session_id, name=name)
    if kind == "seat":
        owner_values = _status_object(values.get("owner"), f"{place}.owner")
        owner: SwapDirectorOwner | SwapNoDirector
        if owner_values.get("kind") == "director":
            owner = SwapDirectorOwner(kind="director", session_id=_status_string(owner_values, "session_id", f"{place}.owner"))
        elif owner_values.get("kind") == "no director":
            owner = SwapNoDirector(kind="no director")
        else:
            raise AccountsError(f"accounts: shutdown inventory {place}.owner.kind is invalid")
        return SwapSeatSession(kind="seat", session_id=session_id, name=name, owner=owner)
    raise AccountsError(f"accounts: shutdown inventory {place}.kind is invalid")


def _swap_inventories(text: str, old: agent_accounts.KnownClaudeAccount) -> list[SwapInventory]:
    try:
        decoded = cast(object, json.loads(text))
    except ValueError as error:
        raise AccountsError(f"accounts: shutdown inventory is invalid JSON: {error}") from error
    if not isinstance(decoded, list) or not decoded:
        raise AccountsError("accounts: shutdown inventory must be a nonempty list of machines")
    inventories: list[SwapInventory] = []
    for index, value in enumerate(cast(list[object], decoded)):
        place = f"machines[{index}]"
        values = _status_object(value, place)
        machine = _status_string(values, "machine", place)
        if values.get("unreachable") or values.get("status") == "unavailable":
            raise AccountsError(f"accounts: {machine}: old account inventory unavailable")
        login = _status_string(values, "login", place)
        if login.casefold() != old.login.casefold():
            raise AccountsError(f"accounts: {machine}: inventory is not for {old.label}")
        sessions = [_swap_session(item, f"{place}.sessions[{i}]")
                    for i, item in enumerate(_status_list(values, "sessions", place))]
        unattributed: list[SwapUnattributedSession] = []
        for i, item in enumerate(_status_list(values, "unattributed", place)):
            item_place = f"{place}.unattributed[{i}]"
            unknown = _status_object(item, item_place)
            pid = unknown.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, int):
                raise AccountsError(f"accounts: shutdown inventory {item_place}.pid must be an integer")
            unattributed.append(SwapUnattributedSession(
                name=_status_string(unknown, "name", item_place, allow_empty=True) or str(pid), pid=pid,
                reason=_status_string(unknown, "reason", item_place),
            ))
        inventories.append(SwapInventory(
            machine=machine, login=login, label=_status_string(values, "label", place),
            sessions=sessions, unattributed=unattributed,
        ))
    return inventories


def _require_moving_sessions(inventories: list[SwapInventory], old_label: str) -> None:
    # Shutdown status lists this machine first; only its unit role is written.
    for index, found in enumerate(inventories):
        units = {session["session_id"] for session in found["sessions"] if session["kind"] == "unit"}
        for session in found["sessions"]:
            if index == 0 and session["kind"] == "unit":
                continue
            if index == 0 and session["kind"] == "seat":
                owner = session["owner"]
                if owner["kind"] == "director" and owner["session_id"] in units:
                    continue
            raise AccountsError(
                f"accounts: cannot swap {old_label}: {found['machine']} {session['kind']} {session['name']} would not move"
            )
        for unknown in found["unattributed"]:
            raise AccountsError(
                f"accounts: cannot swap {old_label}: {found['machine']} unattributed session {unknown['name']} (pid {unknown['pid']})"
            )


def _shutdown_argv(*arguments: str) -> tuple[str, ...]:
    return (sys.executable, str(SCRIPTS / "shutdown" / "shutdown.py"), *arguments)


@dataclass(frozen=True)
class MoveRefused:
    reason: str


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
class ShowrunnersMoveStartFailed:
    session_ids: tuple[str, ...]
    target: str
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class ShowrunnersMoveDryRun:
    session_ids: tuple[str, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ShowrunnersMoveStarted:
    session_ids: tuple[str, ...]
    target: str
    stdout: str
    stderr: str
    returncode: int


def _move_target(target: str) -> agent_accounts.KnownClaudeAccount | MoveRefused:
    default = account_identity(main_config_dir())
    destination = default if target == "default" else account_identity(account_dir(target))
    if target != "default" and isinstance(default, agent_accounts.KnownClaudeAccount) and (
        target.casefold() == default.label.casefold() or
        isinstance(destination, agent_accounts.KnownClaudeAccount) and
        destination.login.casefold() == default.login.casefold()
    ):
        return MoveRefused(f"{target} is the default account; use default")
    problems = check(target)
    if problems:
        return MoveRefused(problems[0])
    if isinstance(destination, agent_accounts.UnreadableClaudeAccount):
        return MoveRefused(f"account identity for {target} is unreadable")
    return destination


def move_showrunners(source: str, target: str, *, dry_run: bool = False, why: str = "") -> ShowrunnersMoveStarted | ShowrunnersMoveStartFailed | ShowrunnersMoveDryRun | MoveRefused:
    """Move all local showrunners on one login through one selected shutdown."""
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    if TYPE_CHECKING:
        from scripts.shutdown import inventory, record
    else:
        inventory = import_module("inventory")
        record = import_module("record")
    try:
        current = account.named_claude_account(source)
        destination = _move_target(target)
        if isinstance(destination, MoveRefused):
            return destination
        if destination.login.casefold() == current.login.casefold():
            return MoveRefused(f"{source} is already on {destination.label}")
        owners = [item for item in inventory.inventory(current.login)["sessions"] if item["kind"] == "showrunner"]
        if not owners:
            return MoveRefused(f"no showrunner on {source} on this machine")
        open_ids = {saved["session"]["session_id"] for saved in record.all_session_records()}
        skipped = tuple(f"{owner['name']} already has a shutdown in progress" for owner in owners if owner["session_id"] in open_ids)
        selected = [owner for owner in owners if owner["session_id"] not in open_ids]
        ids = tuple(owner["session_id"] for owner in selected)
        if not ids:
            return ShowrunnersMoveDryRun((), skipped) if dry_run else ShowrunnersMoveStarted((), target, "\n".join(skipped) + "\n", "", 0)
        reason = why or f"{current.label} is out of usage"
        command = _shutdown_argv("down", source, "--here", "--only", ",".join(ids), "--restart", "--to", target, "--why", reason)
        restart = cast(ManualMoveResume, cast(object, import_module("restart")))
        if dry_run:
            return ShowrunnersMoveDryRun(ids, (*skipped, *(f"{owner['name']}: {owner['session_id']}" for owner in selected),
                shlex.join(command), *(restart.resume_command(owner, target, current.label, why) for owner in selected)))
        for owner in selected:
            if owner["host"]["kind"] == "unknown":
                print(f"run this once {owner['name']} has exited: {restart.resume_command(owner, target, current.label, why)}", flush=True)
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        prefix = "\n".join(skipped) + "\n" if skipped else ""
        outcome = ShowrunnersMoveStartFailed if result.returncode else ShowrunnersMoveStarted
        return outcome(ids, target, prefix + result.stdout, result.stderr, result.returncode)
    except (AccountsError, account.UnknownAccountName, record.InvalidRecord, inventory.InvalidInventory, OSError, ValueError) as error:
        return MoveRefused(str(error))


def move(session: str, target: str, *, why: str = "") -> MoveStarted | MoveStartFailed | MoveRefused:
    """Move one local live owner after completing every read-only refusal check."""
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    if TYPE_CHECKING:
        from scripts.shutdown import inventory, record
        from scripts.message import sessions
    else:
        inventory = import_module("inventory")
        record = import_module("record")
        sessions = import_module("sessions")

    try:
        live = sessions.live_sessions()
        matches = [item for item in live if item["sessionId"] == session]
        if not matches:
            matches = [item for item in live if item["name"] == session]
        if not matches:
            return MoveRefused(f"no live session by id or name {session} on this machine; a session on the other machine moves from there")
        if len(matches) != 1:
            return MoveRefused(f"{session} names multiple live sessions: " + ", ".join(item["sessionId"] for item in matches))
        found = matches[0]
        directory = account.process_config_dir(found["pid"])
        if directory is None:
            return MoveRefused(f"{found['name'] or session}: account unreadable")
        current = account_identity(directory)
        if isinstance(current, agent_accounts.UnreadableClaudeAccount):
            return MoveRefused(f"{found['name'] or session}: account unreadable")
        report = inventory.inventory(current.login, frozenset((found["sessionId"],)))
        owners = [item for item in report["sessions"] if item["session_id"] == found["sessionId"]]
        if not owners:
            return MoveRefused(f"{found['name'] or session}: live session account could not be confirmed")
        owner = owners[0]
        name = owner["name"] or owner["session_id"]
        if owner["kind"] == "seat":
            return MoveRefused("seats move with their unit director")
        destination = _move_target(target)
        if isinstance(destination, MoveRefused):
            return destination
        if destination.login.casefold() == current.login.casefold():
            return MoveRefused(f"{name} is already on {destination.label}")
        if any(saved["session"]["session_id"] == owner["session_id"] for saved in record.all_session_records()):
            return MoveRefused(f"{name} already has a shutdown in progress")
        if owner["host"]["kind"] == "unknown":
            restart = cast(ManualMoveResume, cast(object, import_module("restart")))
            print(f"run this once {name} has exited: {restart.resume_command(owner, target, current.label, why)}", flush=True)
        command = _shutdown_argv("down", current.label, "--here", "--only", owner["session_id"], "--restart", "--to", target, "--why", why or f"{current.label} is out of usage")
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        outcome = MoveStartFailed if result.returncode else MoveStarted
        return outcome(owner["session_id"], target, result.stdout, result.stderr, result.returncode)
    except (AccountsError, record.InvalidRecord, inventory.InvalidInventory, OSError, ValueError) as error:
        return MoveRefused(str(error))


def check_codex_target(label: str, seats_home: Path) -> SelectableCodexAccount:
    home = accounts_home() / ".codex" if label == "default" else codex_account_home(label)
    problems = (_check_default_codex_home(seats_home) if label == "default"
                else _check_codex_home(label, seats_home))
    if problems:
        problem = problems[0].removeprefix("accounts: ")
        raise AccountsError(f"accounts: cannot select {label} ({_codex_folder(home)}): {problem}")
    login = _codex_home_login(home)
    if not login:
        raise AccountsError(f"accounts: {label} ({_codex_folder(home)}) has no readable sign-in; check it and try again")
    return SelectableCodexAccount(home, login, account.label_for("Codex", login))


def codex_home_label(home: Path, login: str | None = None) -> str:
    """The signed-in account's note label, or the numbered home's intended account."""
    if login:
        return account.label_for("Codex", login)
    match = re.fullmatch(r"\.codex([1-9][0-9]*)", home.name)
    if home.parent == accounts_home() and match:
        return agent_accounts.codex_home_role_label(home)
    return NO_VALUE


def _retire_seats_servers(home: Path, label: str, *, dry_run: bool) -> list[str]:
    from scripts.agents.codex_mesh import retire_seats_servers

    return retire_seats_servers(home, label, dry_run=dry_run)


def _swap_seats(label: str, *, dry_run: bool) -> SeatsSwapOutcome:
    seats_home = _seats_home()
    target = check_codex_target(label, seats_home)
    account_label, target_home = target.label, target.home
    if target_home == seats_home:
        raise AccountsError(f"accounts: {account_label} ({_codex_folder(target_home)}) already serves seats; choose another account")
    report = _codex_live(target_home)
    if report.problem:
        raise AccountsError(f"accounts: cannot use {account_label} ({_codex_folder(target_home)}): {report.problem}")
    if (not report.email or report.email.casefold() != target.login.casefold()
            or _codex_home_login(target_home).casefold() != target.login.casefold()):
        raise AccountsError(f"accounts: cannot read {account_label} ({_codex_folder(target_home)}); check its sign-in and try again")
    readiness = agent_accounts.codex_can_work(report, datetime.now(timezone.utc))
    if isinstance(readiness, agent_accounts.CodexWorkExhausted):
        raise AccountsError(f"accounts: {account_label} ({_codex_folder(target_home)}) is used up with no credits; choose another account or wait for reset")
    if isinstance(readiness, agent_accounts.CodexWorkUnknown):
        raise AccountsError(f"accounts: cannot use {account_label} ({_codex_folder(target_home)}): {readiness.problem}")
    if dry_run:
        lines = _retire_seats_servers(target_home, account_label, dry_run=True)
        return SeatsSwapDryRun(tuple([
            f"Would move seats to {account_label} ({_codex_folder(target_home)}); live turns finish first.", *lines,
        ]))
    _ = _write_role("seats", label, account_label)
    lines = _retire_seats_servers(target_home, account_label, dry_run=False)
    return SeatsSwapStarted(tuple([
        f"Seats now use {account_label} ({_codex_folder(target_home)}); working seats move at their next turn.", *lines,
    ]))


def swap(role: str, label: str, *, dry_run: bool = False) -> SwapOutcome | SeatsSwapOutcome:
    """Select the new unit account only after every old-account session can move."""
    _validate_role(role)
    if role == "seats":
        return _swap_seats(label, dry_run=dry_run)
    if label != "default":
        _ = account_dir(label)
    selection = claude_account_selection()
    old = selection.unit_account
    if isinstance(old, agent_accounts.UnreadableClaudeAccount):
        raise AccountsError("accounts: current unit account identity is unreadable")
    if label == "default":
        target = selection.default_account
        if isinstance(target, agent_accounts.UnreadableClaudeAccount):
            raise AccountsError("accounts: default account identity is unreadable")
    else:
        default = selection.default_account
        if isinstance(default, agent_accounts.KnownClaudeAccount) and label.casefold() == default.label.casefold():
            raise AccountsError(f"accounts: {label} is the default account; use default")
        for named in selection.named_accounts:
            if named.label == label:
                target = named.identity
                break
        else:
            problems = check(label)
            raise AccountsError(problems[0] if problems else f"accounts: {label} was not in the checked account selection")
        if selection.is_default_login(target.login):
            raise AccountsError(f"accounts: {label} is the default account; use default")
    if target.login.casefold() == old.login.casefold():
        raise AccountsError(f"accounts: {target.label} is already the unit account")
    try:
        status = subprocess.run(_shutdown_argv("status", old.label, "--json"), capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AccountsError(f"accounts: could not inspect {old.label}: {error}") from error
    if status.returncode:
        reason = (status.stderr or status.stdout).strip()
        raise AccountsError(f"accounts: shutdown status {old.label} failed: {reason}")
    inventories = _swap_inventories(status.stdout, old)
    _require_moving_sessions(inventories, old.label)
    sys.path.insert(0, str(SCRIPTS / "shutdown"))
    if TYPE_CHECKING:
        from scripts.shutdown import record
    else:
        record = import_module("record")
    try:
        open_ids = {saved["session"]["session_id"] for saved in record.all_session_records()}
    except (record.InvalidRecord, OSError) as error:
        raise AccountsError(f"accounts: shutdown state unreadable: {error}") from error
    for session in inventories[0]["sessions"]:
        if session["kind"] == "unit" and session["session_id"] in open_ids:
            raise AccountsError(f"accounts: {session['name']} already has a shutdown in progress")
    down_command = _shutdown_argv("down", old.label, "--restart", "--to", label)
    if dry_run:
        return SwapDryRun((sys.executable, str(Path(__file__).resolve()), "set-role", role, label), down_command)
    previous = _write_unit_role(label, target)
    try:
        down = subprocess.run(down_command, capture_output=True, text=True, check=False)
    except OSError as error:
        down = subprocess.CompletedProcess(down_command, 1, "", f"accounts: could not start shutdown: {error}\n")
    if down.returncode:
        try:
            _restore_unit_role(previous, old.label)
        except OSError as error:
            raise AccountsError(f"accounts: could not restore unit account {old.label}: {error}") from error
        return SwapRolledBack(old.label, down.stdout, down.stderr, down.returncode)
    return SwapStarted(old.label, target.label, down.stdout, down.stderr)


def _is_known_private(name: str) -> bool:
    return (
        name in KNOWN_PRIVATE_NAMES
        or name.startswith("daemon-auth-")
        or name.startswith(".oauth_refresh.lock")
    )


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
            accounts.append(NamedAccount(f"claude {match.group(1)}", child))
    return sorted(accounts, key=lambda account: int(account.label.removeprefix("claude ")))


def _folder_role(config_dir: Path) -> str:
    """The sessions a folder's account runs now; `spare` when no role uses it."""
    selected = role_account("unit")
    unit_dir = main_config_dir() if isinstance(selected, DefaultAccount) else selected.dir
    roles: list[agent_accounts.ClaudeRole] = []
    if config_dir == main_config_dir():
        roles.append("showrunners")
    if config_dir == unit_dir:
        roles.append("unit directors")
    return ", ".join(roles) or "spare"


def _status_for(config_dir: Path, folder: str, named: NamedAccount | None, problems: list[str]) -> AccountStatus:
    baseline = _mcp_server_names(accounts_home() / ".claude.json")
    account_is_safe_to_inspect = not config_dir.is_symlink() and not (
        named is not None and (config_dir / ".claude.json").is_symlink()
    )
    # A named folder keeps its own label whatever login it holds; `problems` names a mismatch.
    label = named.label if named is not None else NO_VALUE
    login = ""
    if account_is_safe_to_inspect:
        identity = account_identity(config_dir)
        if isinstance(identity, agent_accounts.KnownClaudeAccount):
            login = identity.login
            if named is None:
                label = identity.label
        config_file = (
            accounts_home() / ".claude.json"
            if config_dir == main_config_dir()
            else config_dir / ".claude.json"
        )
        current = _mcp_server_names(config_file)
        unclassified = _unclassified(config_dir) if named is not None else ()
    else:
        current = baseline
        unclassified = ()
    return AccountStatus(
        folder=folder,
        config_dir=config_dir,
        is_named_account=named is not None,
        is_set_up=True,
        account_is_safe_to_inspect=account_is_safe_to_inspect,
        label=label,
        login=login,
        role=_folder_role(config_dir),
        problems=tuple(problems),
        unclassified=unclassified,
        missing_mcp_servers=tuple(sorted(baseline - current)),
        extra_mcp_servers=tuple(sorted(current - baseline)),
    )


def _label_number(label: str) -> int:
    return int(label.removeprefix("claude "))


def _noted_labels() -> set[str]:
    """Every `claude N` label that has an account note, with or without a folder."""
    return {
        path.stem for path in _account_notes_directory().glob("claude *.md")
        if LABEL_PATTERN.fullmatch(path.stem)
    }


def account_statuses() -> list[AccountStatus]:
    """Inspect the default account, every named account on disk, and every noted account that no folder holds."""
    default_dir = main_config_dir()
    default_problems: list[str] = []
    if not default_dir.exists():
        default_problems.append(f"accounts: {default_dir} is missing")
    elif not default_dir.is_dir():
        default_problems.append(f"accounts: {default_dir} is not a directory")
    else:
        default_problems.extend(_login_problem(default_dir))
    statuses = [_status_for(default_dir, "~/.claude", None, default_problems)]
    on_disk = {named.label: named for named in _existing_named_accounts()}
    for label in sorted({*on_disk, *_noted_labels()}, key=_label_number):
        folder = f"~/.claude{_label_number(label)}"
        named = on_disk.get(label)
        if named is not None:
            statuses.append(_status_for(named.dir, folder, named, check(label)))
            continue
        if label == statuses[0].label:
            # The default folder holds this account; a second row would repeat its label.
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


def codex_statuses(seats_home: Path) -> list[CodexStatus]:
    """Every listed home, then unheld noted accounts whose home is not listed."""
    statuses: list[CodexStatus] = []
    homes = agent_accounts.codex_homes(seats_home)
    for home in homes:
        folder = _codex_folder(home)
        safe = not home.is_symlink() and not (home / "auth.json").is_symlink()
        login = _codex_home_login(home)
        label = codex_home_label(home, login or None)
        match = re.fullmatch(r"\.codex([1-9][0-9]*)", home.name) if home.parent == accounts_home() else None
        problems = _check_codex_home(f"codex {match.group(1)}", seats_home) if match else []
        if match and not login:
            problems.append(f"accounts: sign in as {label} at {folder}")
        statuses.append(CodexStatus(
            folder, home, True, safe, NO_VALUE if label == login else label, login,
            CODEX_ROLE if home == seats_home else "spare", tuple(problems),
        ))
    held_logins = {status.login.casefold() for status in statuses if status.login}
    noted = sorted(
        (path.stem for path in _account_notes_directory().glob("codex *.md") if CODEX_LABEL_PATTERN.fullmatch(path.stem)),
        key=lambda stem: int(stem.removeprefix("codex ")),
    )
    for label in noted:
        home = codex_account_home(label)
        expected = _account_note(label)
        if home in homes or (
            isinstance(expected, ExpectedAccountLogin) and expected.login.casefold() in held_logins
        ):
            continue
        problems = tuple(_check_codex_home(label, seats_home)) if os.path.lexists(home) else ()
        statuses.append(CodexStatus(
            f"~/{home.name}", home, False, not home.is_symlink(), label, "", NO_VALUE, problems,
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


def _stored_usage(
    statuses: list[AccountStatus], codex: list[CodexStatus], now: datetime,
) -> dict[str, usage_store.StoredUsage]:
    """Each login's stored readings. A login with none read lately is read live first, all at once."""
    stored = usage_store.load()
    stale = [
        status.config_dir for status in statuses
        if status.login and _is_stale(stored.get(usage_store.key("Claude", status.login)), now)
    ]
    stale_codex = [status.home for status in codex
                   if status.login and _is_stale(stored.get(usage_store.key("Codex", status.login)), now)]
    if stale or stale_codex:
        with ThreadPoolExecutor() as pool:
            reads = [pool.submit(agent_accounts.claude_live, config_dir) for config_dir in stale]
            reads.extend(pool.submit(_codex_live, home) for home in stale_codex)
            reports = [read.result() for read in reads]
        for report in reports:
            startup = report.codex_startup
            if isinstance(startup, agent_accounts.CodexStartupFailure):
                for index, status in enumerate(codex):
                    if status.home == startup.home:
                        codex[index] = replace(status, startup=startup, problems=(*status.problems, startup.problem))
        if usage_store.merge(stored, reports, now):
            try:
                usage_store.write(stored)
            except OSError:
                # The table still shows what was read; only the next run reads it again.
                pass
    return stored


def _is_current(window: usage_store.StoredWindow, now: datetime) -> bool:
    """Whether a stored window still describes now. One that has ended says nothing about the one after it."""
    if not window["resets"]:
        return True
    resets = _stored_time(window["resets"])
    return resets is not None and resets > now


def _window_cells(
    window: usage_store.StoredWindow | None, now: datetime, zone: tzinfo | None, pattern: str,
) -> tuple[str, str]:
    """What is left of one window and when it resets."""
    if window is None or not _is_current(window, now):
        return ("unavailable", "unavailable")
    resets = _stored_time(window["resets"])
    return (f"{window['left']:.0f}%", NO_VALUE if resets is None else resets.astimezone(zone).strftime(pattern))


def _credit_cells(usage: usage_store.StoredUsage, now: datetime) -> tuple[str, str]:
    """Whether Codex work now draws on the account's credits, and the credits left.

    Codex spends credits once an allowance window is used up.
    """
    credits = usage.get("credits")
    if credits is None:
        return ("unavailable", "unavailable")
    balance = credits["balance"]
    try:
        amount = math.inf if balance == "unlimited" else float(balance)
    except ValueError:
        # Codex reported no balance.
        return ("unavailable", "unavailable")
    left = "unlimited" if math.isinf(amount) else f"{amount:,.0f}"
    windows = [window for window in (usage.get("five_hour"), usage.get("weekly")) if window is not None]
    current = [window for window in windows if _is_current(window, now)]
    if any(window["left"] <= 0 for window in current):
        return ("yes" if amount > 0 else "no", left)
    return ("no" if "weekly" in usage and len(current) == len(windows) else "unavailable", left)


def _reading_cells(
    usage: usage_store.StoredUsage | None, now: datetime, zone: tzinfo | None, *, has_credits: bool,
) -> tuple[str, ...]:
    """The cells after the role: both windows, the credits, and when they were read."""
    held = usage or usage_store.StoredUsage()
    read = _read_at(usage)
    has_no_five_hour_window = "weekly" in held and "five_hour" not in held
    return (
        *((NO_VALUE, NO_VALUE) if has_no_five_hour_window else _window_cells(held.get("five_hour"), now, zone, "%H:%M %Z")),
        *_window_cells(held.get("weekly"), now, zone, "%a %m-%d %H:%M %Z"),
        *(_credit_cells(held, now) if has_credits else (NO_VALUE, NO_VALUE)),
        NO_VALUE if read is None else read.astimezone(zone).strftime("%H:%M %Z"),
    )


NO_READING = (NO_VALUE,) * 7


def _table_row(
    status: AccountStatus, usage: usage_store.StoredUsage | None, now: datetime, zone: tzinfo | None,
) -> tuple[str, ...]:
    """One Claude folder's row. `usage` is its login's stored reading, if it has one."""
    if not status.is_set_up:
        return (status.folder, status.label, "not set up", status.role, *NO_READING)
    if not status.account_is_safe_to_inspect:
        return (status.folder, status.label, "unavailable", status.role, *NO_READING)
    if not status.login:
        return (status.folder, status.label, "not logged in", status.role, *NO_READING)
    return (
        status.folder, status.label, status.login, status.role,
        *_reading_cells(usage, now, zone, has_credits=False),
    )


def _codex_row(
    status: CodexStatus, usage: usage_store.StoredUsage | None, now: datetime, zone: tzinfo | None,
) -> tuple[str, ...]:
    """One Codex row. `usage` is its login's stored reading, if it has one."""
    if not status.is_set_up:
        return (status.folder, status.label, "not set up", status.role, *NO_READING)
    if not status.account_is_safe_to_inspect:
        return (status.folder, status.label, "unavailable", status.role, *NO_READING)
    if not status.login:
        return (status.folder, status.label, "not logged in", status.role, *NO_READING)
    if isinstance(status.startup, agent_accounts.CodexStartupFailure):
        return (status.folder, status.label, status.login, status.role, status.startup.setup_command, *NO_READING[1:])
    return (
        status.folder, status.label, status.login, status.role,
        *_reading_cells(usage, now, zone, has_credits=True),
    )


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


def _status_notes(status: AccountStatus) -> list[str]:
    """What is wrong or unusual in one folder. Nothing for a folder in order."""
    notes = list(status.problems)
    if status.unclassified:
        notes.append(f"{status.folder}: unclassified: {', '.join(status.unclassified)}")
    mcp_differences = [
        *(f"- {name}" for name in status.missing_mcp_servers),
        *(f"+ {name}" for name in status.extra_mcp_servers),
    ]
    if mcp_differences:
        notes.append(f"{status.folder}: mcpServers: {', '.join(mcp_differences)}")
    return notes


def status_report() -> str:
    """One table row per account folder, then one line for each thing wrong in a folder."""
    statuses = account_statuses()
    seats_home = _seats_home()
    codex = codex_statuses(seats_home)
    now = datetime.now(timezone.utc)
    usage = _stored_usage(statuses, codex, now)
    zone = agent_accounts.user_zone()
    table = _render_table([
        *(_table_row(status, usage.get(usage_store.key("Claude", status.login)), now, zone) for status in statuses),
        *(_codex_row(status, usage.get(usage_store.key("Codex", status.login)), now, zone) for status in codex),
    ])
    notes = [*(note for status in statuses for note in _status_notes(status)),
             *(problem for status in codex for problem in status.problems)]
    return "\n".join([table, *([""] if notes else []), *notes])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action")
    parser.set_defaults(action="status")
    for action in ("setup", "check"):
        command = commands.add_parser(action)
        _ = command.add_argument("label")
    _ = commands.add_parser("status")
    exec_command = commands.add_parser("exec")
    _ = exec_command.add_argument("label")
    _ = exec_command.add_argument("claude_args", nargs=argparse.REMAINDER)
    move_command = commands.add_parser("move")
    _ = move_command.add_argument("session")
    _ = move_command.add_argument("label", nargs="?", default="")
    _ = move_command.add_argument("--from", dest="source", default="")
    _ = move_command.add_argument("--to", dest="target", default="")
    _ = move_command.add_argument("--dry-run", action="store_true")
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


def main(argv: list[str]) -> int:
    options = cast(CommandLine, _parser().parse_args(argv))
    try:
        if options.action == "exec":
            exec_account(options.label, options.claude_args)
            return 0
        if options.action == "move":
            if options.session == "showrunners":
                if not options.source or not options.target or options.label:
                    raise AccountsError("move showrunners requires --from <label> --to <target>")
                moved = move_showrunners(options.source, options.target, dry_run=options.dry_run, why=options.why)
            else:
                if not options.label or options.source or options.target or options.dry_run:
                    raise AccountsError("move requires <session-id|name> <claude N|default>")
                moved = move(options.session, options.label, why=options.why)
            if isinstance(moved, MoveRefused):
                print(moved.reason, file=sys.stderr)
                return 1
            if isinstance(moved, ShowrunnersMoveDryRun):
                print("\n".join(moved.lines))
                return 0
            _ = sys.stdout.write(moved.stdout)
            _ = sys.stderr.write(moved.stderr)
            return moved.returncode
        if options.action == "setup":
            problems = setup(options.label)
            for problem in problems:
                print(problem, file=sys.stderr)
            if CODEX_LABEL_PATTERN.fullmatch(options.label):
                home = codex_account_home(options.label)
                if not problems and home.is_dir() and not home.is_symlink():
                    print(f"CODEX_HOME=~/{home.name} codex login")
                    print("Keep CODEX_HOME on that command: codex login without it signs ~/.codex out and in; seats use that home unless their role names another.")
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
            print(status_report())
            return 0
        if options.action == "role":
            selected = role_account(options.role)
            print("default" if isinstance(selected, DefaultAccount) else selected.label)
            return 0
        if options.action == "env":
            print(launch_prefix(options.role))
            return 0
        if options.action == "set-role":
            set_role(options.role, options.label)
            return 0
        if options.action == "swap":
            result = swap(options.role, options.label, dry_run=options.dry_run)
            if isinstance(result, (SeatsSwapDryRun, SeatsSwapStarted)):
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
                roles = ", ".join(result.roles) or ("spare" if result.label.startswith("claude ") else "unassigned")
                print(
                    f"{result.label} ({roles}): {result.points_per_day:g} points/day "
                    + f"({result.points:g} points over {result.observed_days:g} observed days)"
                )
            if not results:
                print("No paired quota readings in this window.")
            return 0
        raise AccountsError(f"accounts: unknown action {options.action}")
    except AccountsError as error:
        print(error, file=sys.stderr)
        return 2 if options.action == "exec" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
