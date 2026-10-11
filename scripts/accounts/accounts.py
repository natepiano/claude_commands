#!/usr/bin/env python3
"""Build, inspect, and select isolated Claude account directories."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(SCRIPTS / "whoami"))

import account  # noqa: E402
import agent_accounts  # noqa: E402


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
    }
)
IDENTITY_ENTRIES = (".credentials.json", ".claude.json", "account-label")
IDENTITY_ENTRIES_NOT_SEEDED = (".credentials.json", "account-label")
LABEL_PATTERN = re.compile(r"claude ([1-9][0-9]*)")


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


class LegacyRoleChange(TypedDict):
    at: str
    role: str
    label: str


class RoleChange(LegacyRoleChange):
    account_label: str


HistoricalRoleChange = LegacyRoleChange | RoleChange


@dataclass(frozen=True)
class DefaultAccount:
    """The account stored in the default ~/.claude configuration."""


@dataclass(frozen=True)
class NamedAccount:
    """An isolated account directory selected by its claude N label."""

    label: str
    dir: Path


AccountSelection = DefaultAccount | NamedAccount


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

    display_path: str
    is_named_account: bool
    account_is_safe_to_inspect: bool
    label: str
    login: str
    roles: tuple[str, ...]
    problems: tuple[str, ...]
    unclassified: tuple[str, ...]
    missing_mcp_servers: tuple[str, ...]
    extra_mcp_servers: tuple[str, ...]


class CommandLine(argparse.Namespace):
    action: str = ""
    label: str = ""
    role: str = ""
    since: str = ""
    until: str = ""
    dry_run: bool = False


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
class PreviousUnitRole:
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


def _is_expected_link(path: Path, target: Path) -> bool:
    try:
        return path.is_symlink() and os.readlink(path) == str(target)
    except OSError:
        return False


def _real_entry_problem(path: Path) -> str:
    return (
        f"accounts: {path} is a real file; move it aside or merge it into ~/.claude"
    )


def _repair_link(path: Path, target: Path) -> list[str]:
    if path.is_symlink():
        if _is_expected_link(path, target):
            return []
        path.unlink()
    elif path.exists():
        return [_real_entry_problem(path)]
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
        if name not in ALWAYS_LINKED and not os.path.lexists(target):
            continue
        try:
            problems.extend(_repair_link(destination / name, target))
        except OSError as error:
            problems.append(f"accounts: could not link {destination / name}: {error}")

    problems.extend(_seed_config(destination / ".claude.json"))
    return problems


def _link_problems(destination: Path) -> list[str]:
    problems: list[str] = []
    source_dir = main_config_dir().absolute()
    for name in SHARED:
        target = source_dir / name
        if name not in ALWAYS_LINKED and not os.path.lexists(target):
            continue
        path = destination / name
        if path.is_symlink():
            if not _is_expected_link(path, target):
                try:
                    actual = os.readlink(path)
                except OSError:
                    actual = "unreadable"
                problems.append(
                    f"accounts: {path} points to {actual}, not {target}"
                )
        elif path.exists():
            problems.append(_real_entry_problem(path))
        else:
            problems.append(f"accounts: {path} is missing")
    return problems


def _login_problem(config_dir: Path) -> list[str]:
    if isinstance(account_identity(config_dir), agent_accounts.UnreadableClaudeAccount):
        return [f"accounts: {config_dir} is not logged in"]
    return []


def check(label: str) -> list[str]:
    """Return every reason a named account is not fit to launch."""
    destination = account_dir(label)
    if destination.is_symlink():
        return [
            f"accounts: {destination} is a link; an account dir must be a real directory"
        ]
    if not destination.exists():
        return [f"accounts: {destination} is missing"]
    if not destination.is_dir():
        return [f"accounts: {destination} is not a directory"]
    identity_problems = _identity_link_problems(destination)
    problems = [*identity_problems, *_link_problems(destination)]
    if not (destination / ".claude.json").is_symlink():
        problems.extend(_login_problem(destination))
    return problems


def _validate_role(role: str) -> None:
    if role != "unit":
        raise AccountsError(f"accounts: {role} is not a role; only unit is supported")


def _read_accounts_configuration() -> AccountsConfiguration:
    path = accounts_config_file()
    if not path.exists():
        return AccountsConfiguration()
    try:
        decoded = _read_json_object(path)
    except (OSError, ValueError, TypeError) as error:
        raise AccountsError(f"accounts: could not read {path}: {error}") from error
    if "unit" not in decoded:
        return AccountsConfiguration()
    unit = decoded["unit"]
    if not isinstance(unit, str):
        raise AccountsError(f"accounts: {path} has an invalid unit account")
    _ = account_dir(unit)
    return AccountsConfiguration(unit=unit)


def role_account(role: str) -> AccountSelection:
    """Resolve a launch role to the default or one named account."""
    _validate_role(role)
    label = _read_accounts_configuration().get("unit")
    if label is None:
        return DefaultAccount()
    return NamedAccount(label, account_dir(label))


def account_prefix(selected: AccountSelection) -> str:
    """Return the shell assignment for an already selected account."""
    if isinstance(selected, DefaultAccount):
        return ""
    return f"CLAUDE_CONFIG_DIR={shlex.quote(str(selected.dir))} "


def launch_prefix(role: str) -> str:
    """Return the shell assignment needed before launching this role."""
    return account_prefix(role_account(role))


def multiple_accounts_configured() -> bool:
    """Whether account configuration or a named account opts into role displays."""
    return accounts_config_file().exists() or bool(_existing_named_accounts())


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
        from zoneinfo import ZoneInfo

        at = at.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
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
            if values.get("role") != "unit":
                raise ValueError("invalid role")
            label, stamp = values.get("label"), values.get("at")
            if not isinstance(label, str) or not isinstance(stamp, str):
                raise ValueError("invalid label or timestamp")
            if label != "default":
                _ = account_dir(label)
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


def _write_unit_role(label: str, identity: agent_accounts.KnownClaudeAccount) -> PreviousUnitRole:
    """Write a selected identity and retain the previous role for rollback."""
    if label == "default":
        configuration = AccountsConfiguration()
    else:
        _ = account_dir(label)
        configuration = AccountsConfiguration(unit=label)
    path = accounts_config_file()
    try:
        previous = _previous_accounts_configuration_file(path)
        previous_label = "default"
        if isinstance(previous, ExistingAccountsConfigurationFile):
            values = cast(object, json.loads(previous.contents))
            if not isinstance(values, dict):
                raise AccountsError(f"accounts: {path} must contain an object")
            prior = cast(dict[str, object], values).get("unit", "default")
            if not isinstance(prior, str):
                raise AccountsError(f"accounts: {path} has an invalid unit account")
            if prior != "default":
                _ = account_dir(prior)
            previous_label = prior
        _atomic_write(
            path,
            json.dumps(configuration, indent=2, sort_keys=True) + "\n",
        )
    except (OSError, ValueError) as error:
        raise AccountsError(f"accounts: could not write {path}: {error}") from error
    try:
        _append_role_change("unit", label, identity.label)
    except OSError as error:
        try:
            _restore_accounts_configuration_file(path, previous)
        except OSError as restore_error:
            raise AccountsError(
                f"accounts: could not record role change ({error}); "
                + f"could not restore {path}: {restore_error}"
            ) from restore_error
        raise AccountsError(f"accounts: could not record role change: {error}") from error
    return PreviousUnitRole(previous_label, previous)


def set_role(role: str, label: str) -> None:
    """Atomically select a readable account and preserve its note identity."""
    _validate_role(role)
    directory = main_config_dir() if label == "default" else account_dir(label)
    identity = account_identity(directory)
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


def swap(role: str, label: str, *, dry_run: bool = False) -> SwapOutcome:
    """Select the new unit account only after every old-account session can move."""
    _validate_role(role)
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
    _require_moving_sessions(_swap_inventories(status.stdout, old), old.label)
    down_command = _shutdown_argv("down", old.label, "--restart")
    if dry_run:
        return SwapDryRun((sys.executable, str(Path(__file__).resolve()), "set-role", role, label), down_command)
    previous = _write_unit_role(label, target)
    try:
        down = subprocess.run(down_command, capture_output=True, text=True, check=False)
    except OSError as error:
        down = subprocess.CompletedProcess(down_command, 1, "", f"accounts: could not start shutdown: {error}\n")
    if down.returncode:
        try:
            _restore_accounts_configuration_file(accounts_config_file(), previous.configuration)
            _append_role_change(role, previous.label, old.label)
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


def _roles_by_directory() -> dict[Path, tuple[str, ...]]:
    selected = role_account("unit")
    directory = main_config_dir() if isinstance(selected, DefaultAccount) else selected.dir
    return {directory: ("unit",)}


def _status_for(
    config_dir: Path,
    display_path: str,
    is_named_account: bool,
    roles: tuple[str, ...],
    problems: list[str],
) -> AccountStatus:
    baseline = _mcp_server_names(accounts_home() / ".claude.json")
    account_is_safe_to_inspect = not config_dir.is_symlink() and not (
        is_named_account and (config_dir / ".claude.json").is_symlink()
    )
    if account_is_safe_to_inspect:
        identity = account_identity(config_dir)
        login = identity.login if isinstance(identity, agent_accounts.KnownClaudeAccount) else "unavailable"
        account_label = identity.label if isinstance(identity, agent_accounts.KnownClaudeAccount) else "unavailable"
        config_file = (
            accounts_home() / ".claude.json"
            if config_dir == main_config_dir()
            else config_dir / ".claude.json"
        )
        current = _mcp_server_names(config_file)
        unclassified = _unclassified(config_dir) if is_named_account else ()
    else:
        login = "unavailable"
        account_label = "unavailable"
        current = baseline
        unclassified = ()
    return AccountStatus(
        display_path=display_path,
        is_named_account=is_named_account,
        account_is_safe_to_inspect=account_is_safe_to_inspect,
        label=account_label,
        login=login,
        roles=roles,
        problems=tuple(problems),
        unclassified=unclassified,
        missing_mcp_servers=tuple(sorted(baseline - current)),
        extra_mcp_servers=tuple(sorted(current - baseline)),
    )


def account_statuses() -> list[AccountStatus]:
    """Inspect the default account and every named account already on disk."""
    roles = _roles_by_directory()
    default_dir = main_config_dir()
    default_problems: list[str] = []
    if not default_dir.exists():
        default_problems.append(f"accounts: {default_dir} is missing")
    elif not default_dir.is_dir():
        default_problems.append(f"accounts: {default_dir} is not a directory")
    else:
        default_problems.extend(_login_problem(default_dir))
    statuses = [
        _status_for(
            default_dir,
            f"~/.claude ({default_dir})",
            False,
            roles.get(default_dir, ()),
            default_problems,
        )
    ]
    for named in _existing_named_accounts():
        statuses.append(
            _status_for(
                named.dir,
                f"~/.claude{named.label.removeprefix('claude ')}",
                True,
                roles.get(named.dir, ()),
                check(named.label),
            )
        )
    return statuses


def _render_status(status: AccountStatus) -> str:
    roles = ", ".join(status.roles) if status.roles else "none"
    problems = "ok" if not status.problems else "\n".join(
        f"    {problem}" for problem in status.problems
    )
    mcp_differences = [
        *(f"- {name}" for name in status.missing_mcp_servers),
        *(f"+ {name}" for name in status.extra_mcp_servers),
    ]
    mcp_servers = (
        "unavailable"
        if not status.account_is_safe_to_inspect
        else ", ".join(mcp_differences) if mcp_differences else "same"
    )
    lines = [
        status.display_path,
        f"  label: {status.label}",
        f"  login: {status.login}",
        f"  role: {roles}",
        f"  check: {problems}",
    ]
    if status.is_named_account:
        unclassified = ", ".join(status.unclassified) if status.unclassified else "none"
        lines.append(f"  unclassified: {unclassified}")
    lines.append(f"  mcpServers: {mcp_servers}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("setup", "check"):
        command = commands.add_parser(action)
        _ = command.add_argument("label")
    _ = commands.add_parser("status")
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
        if options.action == "setup":
            problems = setup(options.label)
            destination = account_dir(options.label)
            for problem in problems:
                print(problem, file=sys.stderr)
            print(f"CLAUDE_CONFIG_DIR={shlex.quote(str(destination))} claude")
            print("/login")
            return 1 if problems else 0
        if options.action == "check":
            problems = check(options.label)
            for problem in problems:
                print(problem)
            return 1 if problems else 0
        if options.action == "status":
            print("\n\n".join(_render_status(status) for status in account_statuses()))
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
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
