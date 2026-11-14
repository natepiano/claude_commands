"""Decide whether a step may run on a remote target, without the offload runner.

build_targets asks this module first. A refused step never imports offload.py.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import time
from typing import Literal, NewType, cast
from urllib.parse import quote

from scripts.build_targets import target_config
from scripts.buildlog import repository, store
from scripts.lib import conf
from scripts.mac_test import cargo_manifest


MacVerb = Literal["nextest", "check", "clippy", "mend-check", "doc", "build"]


NonBusyMacDeclineReason = Literal[
    "off",
    "repo",
    "linux_only",
    "backoff",
    "blocked",
    "state",
    "unreachable",
    "mac_busy",
    "target does not permit this command",
    "disk",
    "copy",
    "source_changed",
]


LegacyMirrorName = NewType("LegacyMirrorName", str)


RepositoryPolicyName = NewType("RepositoryPolicyName", str)


@dataclass(frozen=True)
class KnownRepository:
    """A repository with its readable name and normalized origin identity."""

    name: str
    key: str = ""

    @property
    def legacy_mirror_name(self) -> LegacyMirrorName:
        return LegacyMirrorName(self.name)

    @property
    def policy_name(self) -> RepositoryPolicyName:
        if self.key and not self.key.startswith("git-root:"):
            return RepositoryPolicyName(self.key.rsplit("/", 1)[-1])
        return RepositoryPolicyName(self.name)

    @property
    def path_key(self) -> str:
        return quote(self.key or self.name, safe="").replace(".", "%2E")


class UnknownRepository:
    """A directory that has no readable git repository identity."""


RepositoryIdentity = KnownRepository | UnknownRepository


UNKNOWN_REPOSITORY = UnknownRepository()


@dataclass(frozen=True)
class DefaultMacSettings:
    """Select the live Mac configuration unless an explicit settings path is supplied."""


DEFAULT_MAC_SETTINGS = DefaultMacSettings()


@dataclass(frozen=True)
class SshConnectionSettings:
    host: str
    connect_timeout_s: float
    leftover_timeout_s: float


@dataclass(frozen=True)
class OffloadConfig:
    """Settings that govern one remote execution decision."""

    enabled: bool
    connection: SshConnectionSettings
    probe_timeout_s: float
    unreachable_backoff_s: float
    free_floor_gib: float
    default_target_budget_gib: str
    values: dict[str, str]
    source: Path | DefaultMacSettings = DEFAULT_MAC_SETTINGS

    @property
    def host(self) -> str:
        return self.connection.host

    @property
    def connect_timeout_s(self) -> float:
        return self.connection.connect_timeout_s

    @property
    def leftover_timeout_s(self) -> float:
        return self.connection.leftover_timeout_s

    @property
    def fetch_limit_mib(self) -> float:
        return float(self.values["fetch_limit_mib"])

    @property
    def abandoned_session_minutes(self) -> int:
        return int(self.values["abandoned_session_minutes"])

    @property
    def mirror_busy_backoff_s(self) -> float:
        return float(self.values["mirror_busy_backoff_s"])

    @property
    def fetch_retention_days(self) -> float:
        return float(self.values["fetch_retention_days"])


class NoBackoff:
    """No active unreachable interval exists."""


class ActiveBackoff:
    """The last failed probe still suppresses remote work."""


BackoffState = NoBackoff | ActiveBackoff


NO_BACKOFF = NoBackoff()


ACTIVE_BACKOFF = ActiveBackoff()


def config_path() -> Path:
    return conf.path("mac_test")


def configured_ssh_target(target: str) -> target_config.SshTarget:
    """Resolve an SSH target before access to its coordination state."""
    return target_config.configured_ssh_target(target)


def ssh_target_state_directory(target: str) -> Path:
    """Return the state path without requiring target registration."""
    return target_config.ssh_target_state_directory(target)


def state_directory(target: str = "mac") -> Path:
    target = target or "mac"
    if target != "mac":
        _ = configured_ssh_target(target)
    return ssh_target_state_directory(target)


def read_connection_settings(path: Path | DefaultMacSettings = DEFAULT_MAC_SETTINGS) -> SshConnectionSettings:
    settings = config_path() if isinstance(path, DefaultMacSettings) else path.expanduser()
    return connection_settings(conf.read(settings))


def connection_settings(config: conf.ConfigRead) -> SshConnectionSettings:
    return SshConnectionSettings(config.text("host"), config.number("connect_timeout_s"),
                                 config.number("leftover_timeout_s"))


def read_config(path: Path | DefaultMacSettings = DEFAULT_MAC_SETTINGS) -> OffloadConfig:
    settings = config_path() if isinstance(path, DefaultMacSettings) else path.expanduser()
    config = conf.read(settings)
    values = config.values()
    values["mirror_busy_backoff_s"] = str(conf.read("mac_test").number("mirror_busy_backoff_s"))
    values["abandoned_session_minutes"] = str(conf.read("mac_test").whole("abandoned_session_minutes"))
    return OffloadConfig(
        enabled=config.text("offload") == "on",
        connection=connection_settings(config),
        probe_timeout_s=config.number("probe_timeout_s"),
        unreachable_backoff_s=config.number("unreachable_backoff_s"),
        free_floor_gib=config.number("free_floor_gib"),
        default_target_budget_gib=config.text("target_budget_gib" if "target_budget_gib" in values else "mac_budget_gib"),
        values=values,
        source=settings,
    )


def repository_identity(repo_root: Path) -> RepositoryIdentity:
    name = repository.repo_name(str(repo_root))
    common = repository.git_common_dir(str(repo_root))
    if name is None or common is None:
        return UNKNOWN_REPOSITORY
    source = Path(repository.source_git_dir(common))
    if not (source / "config").is_file():
        return KnownRepository(name)
    key = repository.repository_key(str(source.parent))
    return KnownRepository(name) if key == store.UNKNOWN_IDENTITY else KnownRepository(name, key)


def comma_values(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def backoff_path(target: str = "mac") -> Path:
    return state_directory(target) / "unreachable_until"


def read_backoff(now: float, target: str = "mac") -> BackoffState:
    path = backoff_path(target)
    try:
        until = float(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return NO_BACKOFF
    except (OSError, ValueError):
        return NO_BACKOFF
    if not math.isfinite(until):
        return NO_BACKOFF
    return ACTIVE_BACKOFF if until > now else NO_BACKOFF


@dataclass(frozen=True)
class FailedProbe:
    """The target's last completed probe failed."""


@dataclass(frozen=True)
class NoFailedProbe:
    """The target has no failed probe record."""


FailedProbeState = FailedProbe | NoFailedProbe


def read_failed_probe(target: str = "mac") -> FailedProbeState:
    return FailedProbe() if (state_directory(target) / "failed_probe").exists() else NoFailedProbe()


def mirror_busy_path(repository: str, target: str = "mac") -> Path:
    """Keep one refusal time for each target and repository."""
    return state_directory(target) / f"mirror-busy-{quote(repository, safe='')}.until"


def read_mirror_busy(now: float, repository: str, target: str = "mac") -> BackoffState:
    try:
        until = float(mirror_busy_path(repository, target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NO_BACKOFF
    return ACTIVE_BACKOFF if math.isfinite(until) and until > now else NO_BACKOFF


@dataclass(frozen=True)
class IneligibleMacStep:
    reason: NonBusyMacDeclineReason


def workspace_has_linux_only(repo_root: Path, excluded: set[str]) -> bool:
    # Cargo adds in-tree path dependencies to workspace membership.
    root = repo_root.resolve()
    manifest = cargo_manifest.toml_table(root / "Cargo.toml")
    workspace = cargo_manifest.nested_table(manifest, "workspace")
    shared = cargo_manifest.nested_table(workspace, "dependencies")
    members = set(cargo_manifest.member_directories(root, manifest))
    excluded_paths = cargo_manifest.expanded_directories(root, cargo_manifest.string_sequence(workspace, "exclude"))
    if not members:
        raise ValueError("workspace members cannot be read")
    pending = list(members)
    while pending:
        member = pending.pop()
        values = cargo_manifest.toml_table(member / "Cargo.toml")
        package = cargo_manifest.nested_table(values, "package")
        name = package.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("workspace package name cannot be read")
        if name in excluded:
            return True
        tables = [values]
        for target in cargo_manifest.nested_table(values, "target").values():
            if not isinstance(target, dict):
                raise ValueError("target dependencies cannot be read")
            tables.append(cast(dict[str, object], target))
        for table in tables:
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                for dependency_name, dependency in cargo_manifest.nested_table(table, section).items():
                    if not isinstance(dependency, dict):
                        if not isinstance(dependency, str):
                            raise ValueError("dependency cannot be read")
                        continue
                    definition = cast(dict[str, object], dependency)
                    base = member
                    if definition.get("workspace") is True:
                        inherited = shared.get(dependency_name)
                        if not isinstance(inherited, (str, dict)):
                            raise ValueError("workspace dependency cannot be read")
                        if isinstance(inherited, str):
                            continue
                        definition = cast(dict[str, object], inherited)
                        base = root
                    if "path" not in definition:
                        continue
                    path = definition["path"]
                    if not isinstance(path, str) or not path:
                        raise ValueError("dependency path cannot be read")
                    directory = (base / path).resolve()
                    if not cargo_manifest.within(root, directory) or directory in excluded_paths or directory in members:
                        continue
                    members.add(directory)
                    pending.append(directory)
    return False


def has_linux_only(repo_root: Path, package: str, excluded: set[str]) -> bool:
    """Check a named package alone, or the whole workspace when no package is named.

    linux_only keeps a package's own steps off the Mac. A package that depends on
    one still builds there, because each linux_only crate has macOS dependencies.
    """
    if package:
        return package in excluded
    return workspace_has_linux_only(repo_root, excluded)


def linux_only_policy(config: OffloadConfig, repository: KnownRepository, repo_root: Path,
                      package: str) -> KnownRepository | IneligibleMacStep:
    """Apply the repository's linux_only setting; an unset or unreadable setting refuses the step."""
    key = f"linux_only.{repository.name}"
    if key not in config.values:
        return IneligibleMacStep("repo")
    try:
        if has_linux_only(repo_root, package, comma_values(config.values[key])):
            return IneligibleMacStep("linux_only")
    except (OSError, UnicodeError, ValueError):
        return IneligibleMacStep("repo")
    return repository


def linux_kind_permitted(values: dict[str, str], kind: str) -> bool:
    return all(step in ('test', 'check', 'doc', 'lint', 'example', 'build')
               and step in comma_values(values.get('kinds', '')) for step in remote_step_kinds(kind))


def remote_step_kinds(kind: str) -> tuple[str, ...]:
    """Return the kinds that a command must run on its pinned target."""
    return ('check', 'test') if kind == 'final' else (kind,)


def verb_kind(verb: MacVerb) -> str:
    return "test" if verb == "nextest" else "lint" if verb in ("clippy", "mend-check") else verb


def mac_step_eligibility(config: OffloadConfig, repo_root: Path, package: str,
                         verb: MacVerb, target: str = "", *, kind: str = "") -> KnownRepository | IneligibleMacStep:
    """Share repository, package/workspace and backoff rules with eligible --kind."""
    if not config.enabled:
        return IneligibleMacStep("off")
    repository = repository_identity(repo_root)
    if isinstance(repository, UnknownRepository):
        return IneligibleMacStep("repo")
    if target and not target_config.uses_legacy_mac_layout(configured_ssh_target(target).platform):
        if not linux_kind_permitted(config.values, kind or verb_kind(verb)):
            return IneligibleMacStep("target does not permit this command")
        if isinstance(read_backoff(time.time(), target), ActiveBackoff):
            return IneligibleMacStep("backoff")
        return repository
    policy = linux_only_policy(config, repository, repo_root, package)
    if isinstance(policy, IneligibleMacStep):
        return policy
    if isinstance(read_backoff(time.time(), target), ActiveBackoff):
        return IneligibleMacStep("backoff")
    return repository


def linux_version_decline_path(target: str, repository_name: str) -> Path:
    """Use one decline record for each repository name."""
    return state_directory(target) / f"version-decline-{repository_name}"
