#!/usr/bin/env python3
"""Build, inspect, and select isolated Claude account directories."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(SCRIPTS / "whoami"))

from scripts.whoami import account as account  # noqa: E402
from scripts.whoami.agent_accounts import claude_on_disk  # noqa: E402


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


class RoleChange(TypedDict):
    at: str
    role: str
    label: str


@dataclass(frozen=True)
class DefaultAccount:
    """The account stored in the default ~/.claude configuration."""


@dataclass(frozen=True)
class NamedAccount:
    """An isolated account directory selected by its claude N label."""

    label: str
    dir: Path


AccountSelection = DefaultAccount | NamedAccount


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
    report = claude_on_disk(config_dir)
    if report.email is None:
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


def launch_prefix(role: str) -> str:
    """Return the shell assignment needed before launching this role."""
    selected = role_account(role)
    if isinstance(selected, DefaultAccount):
        return ""
    return f"CLAUDE_CONFIG_DIR={shlex.quote(str(selected.dir))} "


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


def _append_role_change(role: str, label: str) -> None:
    path = roles_history_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    change = RoleChange(
        at=datetime.now(timezone.utc).isoformat(),
        role=role,
        label=label,
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


def set_role(role: str, label: str) -> None:
    """Atomically select an account and record when that role changed."""
    _validate_role(role)
    if label == "default":
        configuration = AccountsConfiguration()
    else:
        _ = account_dir(label)
        configuration = AccountsConfiguration(unit=label)
    path = accounts_config_file()
    try:
        previous = _previous_accounts_configuration_file(path)
        _atomic_write(
            path,
            json.dumps(configuration, indent=2, sort_keys=True) + "\n",
        )
    except OSError as error:
        raise AccountsError(f"accounts: could not write {path}: {error}") from error
    try:
        _append_role_change(role, label)
    except OSError as error:
        try:
            _restore_accounts_configuration_file(path, previous)
        except OSError as restore_error:
            raise AccountsError(
                f"accounts: could not record role change ({error}); "
                + f"could not restore {path}: {restore_error}"
            ) from restore_error
        raise AccountsError(f"accounts: could not record role change: {error}") from error


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
        report = claude_on_disk(config_dir)
        login = report.email or "unavailable"
        account_label = (
            account.label_for("claude", report.email) if report.email else "unavailable"
        )
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
        raise AccountsError(f"accounts: unknown action {options.action}")
    except AccountsError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
