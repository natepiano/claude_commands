"""Read the configured targets without loading coordination state readers."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum, auto
from pathlib import Path
import os
from typing import Literal


SCRIPTS = Path(__file__).resolve().parent.parent
from scripts.lib import conf


Platform = Literal["linux", "macos"]


@dataclass(frozen=True)
class LocalTarget:
    name: str
    platform: Platform


@dataclass(frozen=True)
class SshTarget:
    name: str
    platform: Platform
    settings: Path


Target = LocalTarget | SshTarget


@dataclass(frozen=True)
class AnyEligibleTarget:
    """The step can use any target that supports its repository and package."""


@dataclass(frozen=True)
class LocalRequired:
    reason: str


Routing = AnyEligibleTarget | LocalRequired
ANY_ELIGIBLE_TARGET = AnyEligibleTarget()


@dataclass(frozen=True)
class EligibleTarget:
    target: Target


@dataclass(frozen=True)
class ExcludedTarget:
    target: Target
    reason: str


ConfigError = conf.Unusable


class LiveConfiguration(Enum):
    CURRENT = auto()


@dataclass
class CommandConfiguration:
    """Configuration reads retained only during one short command."""

    targets: dict[Path, tuple[Target, ...]] = field(default_factory=dict)


COMMAND_CONFIGURATION: ContextVar[LiveConfiguration | CommandConfiguration] = ContextVar(
    "build_target_command_configuration", default=LiveConfiguration.CURRENT)


@contextmanager
def command_configuration() -> Generator[None]:
    """Share configuration reads until the command returns."""
    token = COMMAND_CONFIGURATION.set(CommandConfiguration())
    try:
        yield
    finally:
        COMMAND_CONFIGURATION.reset(token)


class MissingSlotCapacity(Enum):
    REQUIRE_CONFIGURED = auto()
    SINGLE_SLOT = auto()


def slot_capacity(values: Mapping[str, str], repository: str, *,
                  missing: MissingSlotCapacity) -> int:
    """Read one positive slot capacity for claims and admission."""
    value = values.get(f"slots.{repository}", values.get("slots", ""))
    if f"slots.{repository}" not in values and "slots" not in values and missing is MissingSlotCapacity.SINGLE_SLOT:
        return 1
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ConfigError(f"slots.{repository}: expected a positive integer")
    return int(value)


def require_target_name(target: str) -> None:
    if target in (".", "..") or Path(target).name != target:
        raise ConfigError(f"invalid target name: {target}")


def ssh_target_state_directory(target: str) -> Path:
    """Return the state path without requiring target registration."""
    require_target_name(target)
    if target != "mac":
        configured_root = os.environ.get("BUILD_TARGETS_STATE_DIR")
        root = Path(configured_root).expanduser() if configured_root else Path.home() / ".local/state/build-targets"
        return root / target
    configured = os.environ.get("MAC_TEST_STATE_DIR", "~/.local/state/mac-test")
    return Path(configured).expanduser()


def uses_legacy_mac_layout(platform: Platform) -> bool:
    """Mac targets retain one mirror and claim per source repository name."""
    return platform == "macos"


def read_targets(path: Path | None = None) -> tuple[Target, ...]:
    config = conf.path("build_targets") if path is None else path
    current = COMMAND_CONFIGURATION.get()
    if isinstance(current, CommandConfiguration) and config in current.targets:
        return current.targets[config]
    values = conf.read(config).values()
    names = tuple(name.strip() for name in values.get("targets", "").split(","))
    if not all(names) or len(set(names)) != len(names):
        raise ConfigError(f"targets: expected distinct target names in {config}")
    targets: list[Target] = []
    for name in names:
        kind = values.get(f"kind.{name}", "")
        if kind not in ("local", "ssh"):
            raise ConfigError(f"kind.{name}: expected local or ssh, got {kind!r}")
        platform = values.get(f"platform.{name}", "")
        if platform not in ("linux", "macos"):
            raise ConfigError(f"platform.{name}: expected linux or macos, got {platform!r}")
        if kind == "local":
            targets.append(LocalTarget(name, platform))
            continue
        key = f"settings.{name}"
        setting = values.get(key, "")
        if not setting:
            raise ConfigError(f"{key}: expected a settings file")
        settings = (config.parent / Path(setting).expanduser()).resolve()
        if not settings.is_file():
            raise ConfigError(f"{key}: settings file does not exist: {settings}")
        if name == 'aws' or settings.name == 'aws_target.conf':
            raise ConfigError('The permanent AWS builder was removed; use cloud gates')
        targets.append(SshTarget(name, platform, settings))
    result = tuple(targets)
    if isinstance(current, CommandConfiguration):
        current.targets[config] = result
    return result


def configured_ssh_target(target: str) -> SshTarget:
    """Resolve an SSH target before access to its coordination state."""
    for configured in read_targets():
        if configured.name == target and isinstance(configured, SshTarget):
            require_target_name(target)
            return configured
    raise ConfigError(f"unknown SSH target: {target}")


def command_target_name(given: str | None) -> str:
    """Resolve a positional target for clear, drain, or release."""
    configuration = conf.read_or_repository("build_targets")
    configured = read_targets(configuration.source)
    selected = given if given is not None else configuration.text("default_target")
    names = {target.name for target in configured}
    names.update(key.removeprefix("sync.hosts.") for key in configuration.values() if key.startswith("sync.hosts."))
    if selected != "all" and selected not in names:
        raise ConfigError(f"unknown target {selected}; configured targets: {', '.join(target.name for target in configured)}")
    return selected
